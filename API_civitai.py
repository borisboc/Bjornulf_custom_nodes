import os
from server import PromptServer
import aiohttp.web as web
import time
import requests
from PIL import Image, ImageSequence, ImageOps
import numpy as np
import torch
from io import BytesIO
import json
import threading
import random
import importlib
import folder_paths
import node_helpers
import hashlib
from folder_paths import get_filename_list, get_full_path, models_dir
import nodes
from pathlib import Path
import subprocess

# ======================
# SHARED UTILITY FUNCTIONS
# ======================

def get_civitai_base_paths():
    """Returns common paths for CivitAI integration"""
    custom_nodes_dir = Path(__file__).parent.parent.parent.parent
    civitai_base_path = custom_nodes_dir / "ComfyUI" / "custom_nodes" / "Bjornulf_custom_nodes" / "civitai"
    return custom_nodes_dir, civitai_base_path, civitai_base_path  # Last one is parsed_models_path

def setup_checkpoint_directory(model_type):
    """Creates and registers checkpoint directory for specific model type"""
    _, _, parsed_models_path = get_civitai_base_paths()
    checkpoint_dir = Path(folder_paths.models_dir) / "checkpoints" / "Bjornulf_civitAI" / model_type
    
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    
    checkpoint_folders = list(folder_paths.folder_names_and_paths["checkpoints"])
    if str(checkpoint_dir) not in checkpoint_folders:
        checkpoint_folders.append(str(checkpoint_dir))
        folder_paths.folder_names_and_paths["checkpoints"] = tuple(checkpoint_folders)
    
    return checkpoint_dir, parsed_models_path

def setup_image_folders(folder_specs, parent_dir=""):
    """Creates and registers image folders for different model types
    
    Args:
        folder_specs: Dictionary of folder_name -> sub_path
        parent_dir: Optional subdirectory to place links under in input folder
    """
    _, civitai_base_path, _ = get_civitai_base_paths()
    
    for folder_name, sub_path in folder_specs.items():
        full_path = civitai_base_path / sub_path
        folder_paths.add_model_folder_path(folder_name, str(full_path))
        create_symlink(full_path, folder_name, parent_dir)

# Code works, tested on linux and windows
def create_symlink(source, target_name, parent_dir=None):
    """Creates a symlink inside the ComfyUI/input directory on Linux and Windows."""
    if os.name == 'nt':  # Windows
        comfyui_input = Path("ComfyUI/input")
    else:
        comfyui_input = Path("input")
        # Ensure the input directory exists
        comfyui_input.mkdir(parents=True, exist_ok=True)

    if parent_dir:
        parent_path = comfyui_input / parent_dir
        parent_path.mkdir(parents=True, exist_ok=True)
        target = parent_path / target_name
    else:
        target = comfyui_input / target_name

    # Windows handling remains unchanged
    if os.name == 'nt':
        if not target.exists():
            try:
                base_path = Path(__file__).resolve().parent  # Get script location
                source_path = base_path / "ComfyUI" / source  # Ensure it points inside ComfyUI
                try:
                    target.symlink_to(source_path, target_is_directory=source_path.is_dir())
                    #print(f"✅ Symlink created: {target} -> {source_path}")
                except OSError:
                    if source_path.is_dir():
                        cmd = [
                            "powershell",
                            "New-Item",
                            "-ItemType",
                            "Junction",
                            "-Path",
                            str(target),
                            "-Value",
                            str(source_path)
                        ]
                        subprocess.run(cmd, check=True, shell=True, 
                                      stdout=subprocess.DEVNULL, 
                                      stderr=subprocess.DEVNULL)
                        #print(f"✅ Junction created: {target} -> {source_path}")
                    else:
                        print(f"❌ Failed to create symlink/junction for {target_name}.")
            except Exception as e:
                print(f"❌ Failed to create symlink for {target_name}: {e}")
    else:  # Linux handling with complete error management
        try:
            # Check if source is already absolute path
            if os.path.isabs(source):
                source_path = Path(source)
                
                # Check if the source exists with the given case
                if not source_path.exists():
                    # Try case variations for Bjornulf/bjornulf part of the path
                    if 'Bjornulf_custom_nodes' in str(source_path):
                        alt_source_path = Path(str(source_path).replace('Bjornulf_custom_nodes', 'bjornulf_custom_nodes'))
                        if alt_source_path.exists():
                            source_path = alt_source_path
                    elif 'bjornulf_custom_nodes' in str(source_path):
                        alt_source_path = Path(str(source_path).replace('bjornulf_custom_nodes', 'Bjornulf_custom_nodes'))
                        if alt_source_path.exists():
                            source_path = alt_source_path
                
                # If still doesn't exist after trying case variations
                if not source_path.exists():
                    print(f"❌ Source path doesn't exist (checked both cases): {source}")
                    return
            else:
                # For relative paths
                source_path = Path(source).absolute()
                if not source_path.exists():
                    print(f"❌ Source path doesn't exist: {source_path}")
                    return
                
            # Force remove target if it exists (regardless of type)
            if target.exists() or target.is_symlink():
                try:
                    if target.is_dir() and not target.is_symlink():
                        import shutil
                        shutil.rmtree(target)
                    else:
                        os.unlink(target)
                except Exception as e:
                    print(f"❌ Failed to remove existing target {target}: {e}")
                    return
            
            # Create the symlink
            try:
                os.symlink(source_path, target, target_is_directory=source_path.is_dir())
                #print(f"✅ Symlink created: {target} -> {source_path}")
            except Exception as e:
                # Try with explicit target_is_directory set based on source
                try:
                    os.symlink(source_path, target, target_is_directory=True)
                    #print(f"✅ Symlink created with explicit directory flag: {target} -> {source_path}")
                except Exception as e2:
                    print(f"❌ Failed to create symlink for {target_name}: {e2}")
                    
        except Exception as e:
            print(f"❌ Failed to create symlink for {target_name}: {e}")

def download_file(url, destination_path, model_name, api_token=None):
    """Universal downloader with progress tracking"""
    headers = {'Authorization': f'Bearer {api_token}'} if api_token else {}
    filename = f"{model_name}.safetensors"
    file_path = Path(destination_path) / filename

    try:
        with requests.get(url, headers=headers, stream=True) as response:
            response.raise_for_status()
            file_size = int(response.headers.get('content-length', 0))
            
            # Initialize progress tracking if file size is known
            if file_size > 0:
                downloaded = 0
                bar_width = 20  # Fixed width for the progress bar
            
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        # Progress bar logic
                        if file_size > 0:
                            downloaded += len(chunk)
                            progress = min(100, int((downloaded / file_size) * 100))
                            num_hashes = int(progress / (100 / bar_width))
                            bar = "[" + "#" * num_hashes + " " * (bar_width - num_hashes) + "]"
                            percentage = f"{progress:3d}%"
                            print(f"\r{bar} {percentage}", end="", flush=True)
            
            # Add a newline after download completes to avoid overwriting
            if file_size > 0:
                print()  # Moves to the next line after completion
                        
        return str(file_path)
    except Exception as e:
        raise RuntimeError(f"Download failed: {str(e)}")

# Set up main checkpoint directory
_, civitai_base_path, parsed_models_path = get_civitai_base_paths()
bjornulf_checkpoint_path = Path(folder_paths.models_dir) / "checkpoints" / "Bjornulf_civitAI"

# Register the main checkpoint folder
checkpoint_folders = list(folder_paths.folder_names_and_paths["checkpoints"])
if str(bjornulf_checkpoint_path) not in checkpoint_folders:
    checkpoint_folders.append(str(bjornulf_checkpoint_path))
    folder_paths.folder_names_and_paths["checkpoints"] = tuple(checkpoint_folders)

# Define image folders
image_folders = {
    "sdxl_1.0": "sdxl_1.0",
    "sd_1.5": "sd_1.5",
    "pony": "pony",
    "flux.1_d": "flux.1_d",
    "flux.1_s": "flux.1_s",
    "lora_sdxl_1.0": "lora_sdxl_1.0",
    "lora_sd_1.5": "lora_sd_1.5",
    "lora_pony": "lora_pony",
    "lora_flux.1_d": "lora_flux.1_d",
    "lora_hunyuan_video": "lora_hunyuan_video",
    # "NSFW_lora_hunyuan_video": "NSFW_lora_hunyuan_video"
}

# Set up image folders using the function, placing links under input/Bjornulf/
setup_image_folders(image_folders)

def get_civitai():
    import civitai
    importlib.reload(civitai)
    return civitai

# Check if the environment variable exists
if "CIVITAI_API_TOKEN" not in os.environ:
    os.environ["CIVITAI_API_TOKEN"] = "d5fc336223a367e6b503a14a10569825"
# else:
#     print("CIVITAI_API_TOKEN already exists in the environment.")
import civitai

# ======================
# GENERATE WITH CIVITAI
# ======================

class APIGenerateCivitAI:
    @classmethod
    def INPUT_TYPES(cls):
        """Define the input types for the node."""
        return {
            "required": {
                "api_token": ("STRING", {"default": "", "placeholder": "CivitAI API token"}),
                "prompt": ("STRING", {"multiline": True, "default": "RAW photo, face portrait photo of 26 y.o woman"}),
                "negative_prompt": ("STRING", {
                    "multiline": True,
                    "default": "low quality, blurry, pixelated, distorted, artifacts"
                }),
                "width": ("INT", {"default": 1024, "min": 128, "max": 1024, "step": 64}),
                "height": ("INT", {"default": 768, "min": 128, "max": 1024, "step": 64}),
                "steps": ("INT", {"default": 20, "min": 1, "max": 50, "step": 1}),
                "cfg_scale": ("FLOAT", {"default": 7.0, "min": 1.0, "max": 30.0, "step": 0.1}),
                "seed": ("INT", {"default": -1, "min": -1, "max": 0x7FFFFFFFFFFFFFFF}),
                "number_of_images": ("INT", {"default": 1, "min": 1, "max": 10, "step": 1}),
                "timeout": ("INT", {"default": 300, "min": 60, "max": 1800, "step": 60}),
            },
            "optional": {
                "model_urn": ("STRING", {"default": "urn:air:sdxl:checkpoint:civitai:101055@128078"}), #SDXL default
                "add_LORA": ("STRING", {"multiline": True, "default": ""}),
                "DO_NOT_WAIT": ("BOOLEAN", {"default": False, "label_on": "Save Links Only", "label_off": "Generate Now"}),
                "links_file": ("STRING", {"default": "", "multiline": False}),
                "LIST_from_style_selector": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "placeholder": "e.g., Low Poly ;Samaritan 3D Cartoon;urn:air:sdxl:checkpoint:civitai:81270@144566;https://civitai.green/models/81270?modelVersionId=144566"
                }),
            }
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("images", "generation_info")
    FUNCTION = "generate"
    CATEGORY = "Civitai"

    def __init__(self):
        self.links_dir = "Bjornulf/civitai_links"
        os.makedirs(self.links_dir, exist_ok=True)
        self._interrupt_event = threading.Event()

    def generate(self, api_token, prompt, negative_prompt, width, height, steps, cfg_scale, seed, number_of_images, timeout, model_urn="", add_LORA="", DO_NOT_WAIT=False, links_file="", LIST_from_style_selector=""):
        """Generate images or save links based on DO_NOT_WAIT."""
        if not api_token:
            raise ValueError("API token is required")
        os.environ["CIVITAI_API_TOKEN"] = api_token
        civitai = get_civitai()

        empty_image = torch.zeros((1, 512, 512, 3))

        # Extract model_urn from LIST_from_style_selector if model_urn is empty and LIST_from_style_selector is provided
        if not model_urn and LIST_from_style_selector:
            parts = LIST_from_style_selector.split(';')
            if len(parts) >= 3:
                model_urn = parts[2].strip()
            else:
                raise ValueError("Invalid LIST_from_style_selector format: cannot extract model_urn")
        if not model_urn:
            raise ValueError("model_urn is required")

        seed = random.randint(0, 0x7FFFFFFFFFFFFFFF) if seed == -1 else seed
        jobs = []

        # Prepare job requests
        for i in range(number_of_images):
            current_seed = seed + i
            input_data = {
                "model": model_urn,
                "params": {
                    "prompt": prompt,
                    "negativePrompt": negative_prompt,
                    "scheduler": "EulerA",
                    "steps": steps,
                    "cfgScale": cfg_scale,
                    "width": width,
                    "height": height,
                    "clipSkip": 2,
                    "seed": current_seed
                }
            }
            if add_LORA:
                try:
                    lora_data = json.loads(add_LORA)
                    if "additionalNetworks" in lora_data:
                        input_data["additionalNetworks"] = lora_data["additionalNetworks"]
                except Exception as e:
                    print(f"Error processing LORA data: {str(e)}")

            response = civitai.image.create(input_data)
            if 'token' not in response or 'jobs' not in response:
                raise ValueError("Invalid API response")
            jobs.append({
                'token': response['token'],
                'job_id': response['jobs'][0]['jobId'],
                'input_data': input_data
            })

        # Save links if DO_NOT_WAIT is True
        if DO_NOT_WAIT:
            date_str = time.strftime("%d_%B_%Y").lower()
            base_name = f"{date_str}_"
            existing_files = [f for f in os.listdir(self.links_dir) if f.startswith(base_name) and f.endswith(".txt")]
            next_number = max([int(f[len(base_name):-4]) for f in existing_files] or [0]) + 1
            file_name = f"{date_str}_{next_number:03d}.txt"
            file_path = os.path.join(self.links_dir, links_file if links_file else file_name)
            mode = 'a' if links_file else 'w'
            if not file_path.endswith(".txt"):
                file_path += ".txt"

            with open(file_path, mode) as f:
                for job in jobs:
                    if LIST_from_style_selector:
                        f.write(f"{LIST_from_style_selector};Token: {job['token']};Job ID: {job['job_id']}\n")
                    else:
                        f.write(f"Token: {job['token']};Job ID: {job['job_id']}\n")

            generation_info = {
                "status": "links_saved",
                "links_file": file_path,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "number_of_jobs": len(jobs)
            }
            return (empty_image, json.dumps(generation_info, indent=2))

        # Generate images immediately (DO_NOT_WAIT=False)
        images = []
        infos = []
        failed_jobs = []

        for job in jobs:
            try:
                image_url = self.check_job_status(job['token'], job['job_id'], timeout)
                image_response = requests.get(image_url)
                if image_response.status_code != 200:
                    raise ConnectionError(f"Image download failed: {image_response.status_code}")

                img = Image.open(BytesIO(image_response.content)).convert('RGB')
                img_tensor = torch.from_numpy(np.array(img).astype(np.float32) / 255.0)
                images.append(img_tensor.unsqueeze(0))
                infos.append(self.format_generation_info(job['input_data'], job['token'], job['job_id'], image_url))

            except Exception as e:
                failed_jobs.append({'job': job, 'error': str(e)})

        if not images:
            generation_info = {"error": "All jobs failed", "failed_jobs": failed_jobs}
            return (empty_image, json.dumps(generation_info, indent=2))

        combined_tensor = torch.cat(images, dim=0)
        combined_info = {
            "successful_generations": len(images),
            "total_requested": number_of_images,
            "individual_results": infos,
            "failed_jobs": failed_jobs if failed_jobs else None
        }
        return (combined_tensor, json.dumps(combined_info, indent=2))

    def check_job_status(self, job_token, job_id, timeout=9999):
        """Check job status with timeout"""
        start_time = time.time()
        while time.time() - start_time < timeout and not self._interrupt_event.is_set():
            try:
                response = civitai.jobs.get(token=job_token)
                job_status = response['jobs'][0]
                
                if job_status.get('status') == 'failed':
                    raise Exception(f"Job failed: {job_status.get('error', 'Unknown error')}")
                
                if job_status['result'].get('available'):
                    return job_status['result'].get('blobUrl')
                
                print(f"Job Status: {job_status['status']}")
                time.sleep(2)
                
            except Exception as e:
                print(f"Error checking job status: {str(e)}")
                time.sleep(2)
            
            # Check for interruption
            if self._interrupt_event.is_set():
                raise InterruptedError("Generation interrupted by user")
        
        if self._interrupt_event.is_set():
            raise InterruptedError("Generation interrupted by user")
        raise TimeoutError(f"Job timed out after {timeout} seconds")

    def format_generation_info(self, input_data, token, job_id, image_url):
        """Format generation info (implementation assumed)."""
        return {"token": token, "job_id": job_id, "image_url": image_url}

class LoadCivitAILinks:
    @classmethod
    def INPUT_TYPES(cls):
        """Define the input types for the node."""
        return {
            "required": {
                "api_token": ("STRING", {"default": "", "placeholder": "CivitAI API token"}),
                "links_file_path": ("STRING", {
                    "default": "",
                    "placeholder": "Path to links file (priority if not empty)"
                }),
                "selected_file": (["Not selected"] + cls.get_links_files(), {
                    "default": "Not selected"
                }),
                "direct_links": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "placeholder": "Enter links directly (e.g., Style;Model;URN;Link;Token: <token>;Job ID: <job_id>)"
                }),
            },
            "optional": {
                "auto_save": ("BOOLEAN", {
                    "default": False,
                    "label_on": "Enable Auto-Save",
                    "label_off": "Disable Auto-Save"
                }),
            }
        }

    RETURN_TYPES = ("IMAGE", "STRING", "STRING")
    RETURN_NAMES = ("images", "status_info", "LIST_style")
    OUTPUT_IS_LIST = (False, False, True)
    FUNCTION = "load_images"
    CATEGORY = "Civitai"

    def __init__(self):
        """Initialize the node with the links directory."""
        self.links_dir = "Bjornulf/civitai_links"
        os.makedirs(self.links_dir, exist_ok=True)

    @classmethod
    def get_links_files(cls):
        links_dir = "Bjornulf/civitai_links"
        if not os.path.exists(links_dir):
            return []
        files = [f for f in os.listdir(links_dir) if f.endswith(".txt")]
        return files

    def load_images(self, api_token, links_file_path, selected_file, direct_links, auto_save=False):
        """Load images from links and optionally save them to style-based folders."""
        if not api_token:
            raise ValueError("API token is required")
        os.environ["CIVITAI_API_TOKEN"] = api_token
        civitai = get_civitai()

        # Determine the source of links
        lines = None
        if links_file_path:
            if not os.path.exists(links_file_path):
                raise ValueError(f"File path '{links_file_path}' does not exist")
            with open(links_file_path, 'r') as f:
                lines = f.readlines()
        elif selected_file != "Not selected":
            file_path = os.path.join(self.links_dir, selected_file)
            if not os.path.exists(file_path):
                raise ValueError(f"Selected file '{file_path}' does not exist")
            with open(file_path, 'r') as f:
                lines = f.readlines()
        elif direct_links:
            lines = direct_links.splitlines()
        else:
            raise ValueError("No valid links source provided")

        images = []
        list_styles = []  # To store LIST_style strings
        status_info = {
            "loaded": 0,
            "failed": 0,
            "attempted": 0,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        }

        for line in lines:
            line = line.strip()
            if not line:
                continue
            status_info["attempted"] += 1
            try:
                parts = line.split(";")
                if len(parts) == 6:
                    style = parts[0].strip()
                    model_name = parts[1].strip()
                    model_urn = parts[2].strip()
                    model_link = parts[3].strip()
                    token = parts[4].split("Token: ")[1].strip()
                    job_id = parts[5].split("Job ID: ")[1].strip()
                    list_style = ';'.join(parts[:4])
                elif len(parts) == 2 and "Token: " in parts[0] and "Job ID: " in parts[1]:
                    token = parts[0].split("Token: ")[1].strip()
                    job_id = parts[1].split("Job ID: ")[1].strip()
                    list_style = ""
                else:
                    raise ValueError(f"Invalid link format: {line}")

                # Fetch job status from CivitAI API
                response = civitai.jobs.get(token=token)
                job_status = next((job for job in response['jobs'] if job['jobId'] == job_id), None)

                if not job_status or not job_status['result'].get('available'):
                    status_info["failed"] += 1
                    continue

                # Download and process the image
                image_url = job_status['result'].get('blobUrl')
                image_response = requests.get(image_url)
                if image_response.status_code != 200:
                    status_info["failed"] += 1
                    continue

                img = Image.open(BytesIO(image_response.content))
                if img.mode != 'RGB':
                    img = img.convert('RGB')

                # Auto-save if enabled and style is available
                if auto_save and len(parts) == 6:
                    style_folder = style.replace(" ", "_")  # Replace spaces with underscores
                    save_dir = os.path.join(folder_paths.get_output_directory(), "civitai_autosave", style_folder)
                    os.makedirs(save_dir, exist_ok=True)
                    file_name = f"{job_id}.png"
                    file_path = os.path.join(save_dir, file_name)
                    img.save(file_path)

                # Convert to tensor and collect
                img_tensor = torch.from_numpy(np.array(img).astype(np.float32) / 255.0)
                images.append(img_tensor.unsqueeze(0))
                list_styles.append(list_style)
                status_info["loaded"] += 1

            except Exception as e:
                status_info["failed"] += 1
                print(f"Error processing link '{line}': {str(e)}")

        if not images:
            raise ValueError("No images loaded from the provided links")

        combined_tensor = torch.cat(images, dim=0)
        return (combined_tensor, json.dumps(status_info, indent=2), list_styles)

    @classmethod
    def IS_CHANGED(cls, api_token, links_file_path, selected_file, direct_links, auto_save):
        """Force node re-execution when inputs change."""
        return float("NaN")

@PromptServer.instance.routes.post("/get_civitai_links_files")
async def get_civitai_links_files(request):
    try:
        links_dir = "Bjornulf/civitai_links"
        if not os.path.exists(links_dir):
            return web.json_response({
                "success": False,
                "error": "Links directory does not exist"
            }, status=404)
        files = [f for f in os.listdir(links_dir) if f.endswith(".txt")]
        return web.json_response({
            "success": True,
            "files": files
        }, status=200)
    except Exception as e:
        error_msg = str(e)
        return web.json_response({
            "success": False,
            "error": error_msg
        }, status=500)
class APIGenerateCivitAIAddLORA:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "lora_urn": ("STRING", {
                    "multiline": False,
                    "default": "urn:air:flux1:lora:civitai:790034@883473"
                }),
                "strength": ("FLOAT", {
                    "default": 1.0,
                    "min": 0.0,
                    "max": 2.0,
                    "step": 0.01
                }),
            },
            "optional": {
                "add_LORA": ("add_LORA", {"forceInput": True}),
            }
        }

    RETURN_TYPES = ("add_LORA",)
    FUNCTION = "add_lora"
    CATEGORY = "Civitai"

    def add_lora(self, lora_urn, strength, add_LORA=None):
        try:
            request_data = {"additionalNetworks": {}}
            
            # Add the new LORA
            request_data["additionalNetworks"][lora_urn] = {
                "type": "Lora",
                "strength": strength
            }
            
            # If add_LORA is provided, concatenate it
            if add_LORA:
                additional_loras = json.loads(add_LORA)
                if "additionalNetworks" in additional_loras:
                    request_data["additionalNetworks"].update(additional_loras["additionalNetworks"])
            
            return (json.dumps(request_data),)
        except Exception as e:
            print(f"Error adding LORA: {str(e)}")
            return (json.dumps({"additionalNetworks": {}}),)

# ======================
# MODEL SELECTOR CLASSES
# ======================

class CivitAIModelSelectorSD15:
    @classmethod
    def INPUT_TYPES(s):
        # Get list of supported image extensions
        image_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp')
        files = [f"sd_1.5/{f}" for f in folder_paths.get_filename_list("sd_1.5") 
                if f.lower().endswith(image_extensions)]
        
        if not files:  # If no files found, provide a default option
            files = ["none"]
            
        return {
            "required": {
                        "image": (sorted(files), {"image_upload": True}),
                        "civitai_token": ("STRING", {"default": ""})
            },
        }

    RETURN_TYPES = ("MODEL", "CLIP", "VAE", "STRING", "STRING")
    RETURN_NAMES = ("model", "clip", "vae", "name", "civitai_url")
    FUNCTION = "load_model"
    CATEGORY = "Bjornulf"
 
    def load_model(self, image, civitai_token):
        if image == "none":
            raise ValueError("No image selected")

        # Get the absolute path to the JSON file
        json_path = os.path.join(parsed_models_path, 'parsed_sd_1.5_models.json')

        # Load models info
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                models_info = json.load(f)
        except UnicodeDecodeError:
            # Fallback to latin-1 if UTF-8 fails
            with open(json_path, 'r', encoding='latin-1') as f:
                models_info = json.load(f)
        
        # Extract model name from image path
        image_name = os.path.basename(image)
        # Find corresponding model info
        model_info = next((model for model in models_info 
                          if os.path.basename(model['image_path']) == image_name), None)
        
        if not model_info:
            raise ValueError(f"No model information found for image: {image_name}")

        # Create checkpoints directory if it doesn't exist
        checkpoint_dir = os.path.join(folder_paths.models_dir, "checkpoints", "Bjornulf_civitAI", "sd1.5")
        os.makedirs(checkpoint_dir, exist_ok=True)

        # Expected model filename
        model_filename = f"{model_info['name']}.safetensors"
        full_model_path = os.path.join(checkpoint_dir, model_filename)

        # Check if model is already downloaded
        if not os.path.exists(full_model_path):
            print(f"Downloading model {model_info['name']}...")
            
            # Construct download URL with token
            download_url = model_info['download_url']
            if civitai_token:
                download_url += f"?token={civitai_token}" if '?' not in download_url else f"&token={civitai_token}"

            try:
                # Download the file using class method
                download_file(download_url, checkpoint_dir, model_info['name'], civitai_token)
            except Exception as e:
                raise ValueError(f"Failed to download model: {e}")

        # Get relative path
        relative_model_path = os.path.join("Bjornulf_civitAI", "sd1.5", model_filename)

        # Try loading with relative path first
        try:
            model = nodes.CheckpointLoaderSimple().load_checkpoint(relative_model_path)
        except Exception as e:
            print(f"Error loading model with relative path: {e}")
            print(f"Attempting to load from full path: {full_model_path}")
            # Fallback to direct loading if needed
            from comfy.sd import load_checkpoint_guess_config
            model = load_checkpoint_guess_config(full_model_path)

        return (model[0], model[1], model[2], model_info['name'], f"https://civitai.com/models/{model_info['model_id']}")

    @classmethod
    def IS_CHANGED(s, image, **kwargs):
        if image == "none":
            return ""
        image_path = os.path.join(civitai_base_path, image)
        if not os.path.exists(image_path):
            return ""
        
        m = hashlib.sha256()
        with open(image_path, 'rb') as f:
            m.update(f.read())
        m.update(image.encode('utf-8'))
        return m.digest().hex()


class CivitAIModelSelectorSDXL:
    @classmethod
    def INPUT_TYPES(s):
        # Get list of supported image extensions
        image_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp')
        files = [f"sdxl_1.0/{f}" for f in folder_paths.get_filename_list("sdxl_1.0") 
                if f.lower().endswith(image_extensions)]
        
        if not files:  # If no files found, provide a default option
            files = ["none"]
            
        return {
            "required": {
                        "image": (sorted(files), {"image_upload": True}),
                        "civitai_token": ("STRING", {"default": ""})
            },
        }

    RETURN_TYPES = ("MODEL", "CLIP", "VAE", "STRING", "STRING")
    RETURN_NAMES = ("model", "clip", "vae", "name", "civitai_url")
    FUNCTION = "load_model"
    CATEGORY = "Bjornulf"
 
    def load_model(self, image, civitai_token):
        create_bjornulf_checkpoint_folder()
        if image == "none":
            raise ValueError("No image selected")

        # Get the absolute path to the JSON file
        json_path = os.path.join(parsed_models_path, 'parsed_sdxl_1.0_models.json')

        # Load models info
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                models_info = json.load(f)
        except UnicodeDecodeError:
            # Fallback to latin-1 if UTF-8 fails
            with open(json_path, 'r', encoding='latin-1') as f:
                models_info = json.load(f)
        
        # Extract model name from image path
        image_name = os.path.basename(image)
        # Find corresponding model info
        model_info = next((model for model in models_info 
                          if os.path.basename(model['image_path']) == image_name), None)
        
        if not model_info:
            raise ValueError(f"No model information found for image: {image_name}")

        # Create checkpoints directory if it doesn't exist
        checkpoint_dir = os.path.join(folder_paths.models_dir, "checkpoints", "Bjornulf_civitAI", "sdxl_1.0")
        os.makedirs(checkpoint_dir, exist_ok=True)

        # Expected model filename
        model_filename = f"{model_info['name']}.safetensors"
        full_model_path = os.path.join(checkpoint_dir, model_filename)

        # Check if model is already downloaded
        if not os.path.exists(full_model_path):
            print(f"Downloading model {model_info['name']}...")
            
            # Construct download URL with token
            download_url = model_info['download_url']
            if civitai_token:
                download_url += f"?token={civitai_token}" if '?' not in download_url else f"&token={civitai_token}"

            try:
                # Download the file using class method
                download_file(download_url, checkpoint_dir, model_info['name'], civitai_token)
            except Exception as e:
                raise ValueError(f"Failed to download model: {e}")

        # Get relative path
        relative_model_path = os.path.join("Bjornulf_civitAI", "sdxl_1.0", model_filename)

        # Try loading with relative path first
        try:
            model = nodes.CheckpointLoaderSimple().load_checkpoint(relative_model_path)
        except Exception as e:
            print(f"Error loading model with relative path: {e}")
            print(f"Attempting to load from full path: {full_model_path}")
            # Fallback to direct loading if needed
            from comfy.sd import load_checkpoint_guess_config
            model = load_checkpoint_guess_config(full_model_path)

        # return (model[0], model[1], model[2], model_info['name'], model_info['download_url'])
        return (model[0], model[1], model[2], model_info['name'], f"https://civitai.com/models/{model_info['model_id']}")

    @classmethod
    def IS_CHANGED(s, image, **kwargs):
        if image == "none":
            return ""
        image_path = os.path.join(civitai_base_path, image)
        if not os.path.exists(image_path):
            return ""
        
        m = hashlib.sha256()
        with open(image_path, 'rb') as f:
            m.update(f.read())
        m.update(image.encode('utf-8'))
        return m.digest().hex()


class CivitAIModelSelectorFLUX_D:
    @classmethod
    def INPUT_TYPES(s):
        # Get list of supported image extensions
        image_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp')
        files = [f"flux.1_d/{f}" for f in folder_paths.get_filename_list("flux.1_d") 
                if f.lower().endswith(image_extensions)]
        
        if not files:  # If no files found, provide a default option
            files = ["none"]
            
        return {
            "required": {
                        "image": (sorted(files), {"image_upload": True}),
                        "civitai_token": ("STRING", {"default": ""})
            },
        }

    RETURN_TYPES = ("MODEL", "CLIP", "VAE", "STRING", "STRING")
    RETURN_NAMES = ("model", "clip", "vae", "name", "civitai_url")
    FUNCTION = "load_model"
    CATEGORY = "Bjornulf"
 
    def load_model(self, image, civitai_token):
        create_bjornulf_checkpoint_folder()
        if image == "none":
            raise ValueError("No image selected")

        # Get the absolute path to the JSON file
        json_path = os.path.join(parsed_models_path, 'parsed_flux.1_d_models.json')

        # Load models info
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                models_info = json.load(f)
        except UnicodeDecodeError:
            # Fallback to latin-1 if UTF-8 fails
            with open(json_path, 'r', encoding='latin-1') as f:
                models_info = json.load(f)
        
        # Extract model name from image path
        image_name = os.path.basename(image)
        # Find corresponding model info
        model_info = next((model for model in models_info 
                          if os.path.basename(model['image_path']) == image_name), None)
        
        if not model_info:
            raise ValueError(f"No model information found for image: {image_name}")

        # Create checkpoints directory if it doesn't exist
        checkpoint_dir = os.path.join(folder_paths.models_dir, "checkpoints", "Bjornulf_civitAI", "flux_d")
        os.makedirs(checkpoint_dir, exist_ok=True)

        # Expected model filename
        model_filename = f"{model_info['name']}.safetensors"
        full_model_path = os.path.join(checkpoint_dir, model_filename)

        # Check if model is already downloaded
        if not os.path.exists(full_model_path):
            print(f"Downloading model {model_info['name']}...")
            
            # Construct download URL with token
            download_url = model_info['download_url']
            if civitai_token:
                download_url += f"?token={civitai_token}" if '?' not in download_url else f"&token={civitai_token}"

            try:
                # Download the file using class method
                download_file(download_url, checkpoint_dir, model_info['name'], civitai_token)
            except Exception as e:
                raise ValueError(f"Failed to download model: {e}")

        # Get relative path
        relative_model_path = os.path.join("Bjornulf_civitAI", "flux_d", model_filename)

        # Try loading with relative path first
        try:
            model = nodes.CheckpointLoaderSimple().load_checkpoint(relative_model_path)
        except Exception as e:
            print(f"Error loading model with relative path: {e}")
            print(f"Attempting to load from full path: {full_model_path}")
            # Fallback to direct loading if needed
            from comfy.sd import load_checkpoint_guess_config
            model = load_checkpoint_guess_config(full_model_path)

        return (model[0], model[1], model[2], model_info['name'], f"https://civitai.com/models/{model_info['model_id']}")

    @classmethod
    def IS_CHANGED(s, image, **kwargs):
        if image == "none":
            return ""
        image_path = os.path.join(civitai_base_path, image)
        if not os.path.exists(image_path):
            return ""
        
        m = hashlib.sha256()
        with open(image_path, 'rb') as f:
            m.update(f.read())
        m.update(image.encode('utf-8'))
        return m.digest().hex()


class CivitAIModelSelectorFLUX_S:
    @classmethod
    def INPUT_TYPES(s):
        # Get list of supported image extensions
        image_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp')
        files = [f"flux.1_s/{f}" for f in folder_paths.get_filename_list("flux.1_s") 
                if f.lower().endswith(image_extensions)]
        
        if not files:  # If no files found, provide a default option
            files = ["none"]
            
        return {
            "required": {
                        "image": (sorted(files), {"image_upload": True}),
                        "civitai_token": ("STRING", {"default": ""})
            },
        }

    RETURN_TYPES = ("MODEL", "CLIP", "VAE", "STRING", "STRING")
    RETURN_NAMES = ("model", "clip", "vae", "name", "civitai_url")
    FUNCTION = "load_model"
    CATEGORY = "Bjornulf"
 
    def load_model(self, image, civitai_token):
        create_bjornulf_checkpoint_folder()
        if image == "none":
            raise ValueError("No image selected")

        # Get the absolute path to the JSON file
        json_path = os.path.join(parsed_models_path, 'parsed_flux.1_s_models.json')

        # Load models info
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                models_info = json.load(f)
        except UnicodeDecodeError:
            # Fallback to latin-1 if UTF-8 fails
            with open(json_path, 'r', encoding='latin-1') as f:
                models_info = json.load(f)
        
        # Extract model name from image path
        image_name = os.path.basename(image)
        # Find corresponding model info
        model_info = next((model for model in models_info 
                          if os.path.basename(model['image_path']) == image_name), None)
        
        if not model_info:
            raise ValueError(f"No model information found for image: {image_name}")

        # Create checkpoints directory if it doesn't exist
        checkpoint_dir = os.path.join(folder_paths.models_dir, "checkpoints", "Bjornulf_civitAI", "flux_s")
        os.makedirs(checkpoint_dir, exist_ok=True)

        # Expected model filename
        model_filename = f"{model_info['name']}.safetensors"
        full_model_path = os.path.join(checkpoint_dir, model_filename)

        # Check if model is already downloaded
        if not os.path.exists(full_model_path):
            print(f"Downloading model {model_info['name']}...")
            
            # Construct download URL with token
            download_url = model_info['download_url']
            if civitai_token:
                download_url += f"?token={civitai_token}" if '?' not in download_url else f"&token={civitai_token}"

            try:
                # Download the file using class method
                download_file(download_url, checkpoint_dir, model_info['name'], civitai_token)
            except Exception as e:
                raise ValueError(f"Failed to download model: {e}")

        # Get relative path
        relative_model_path = os.path.join("Bjornulf_civitAI", "flux_s", model_filename)

        # Try loading with relative path first
        try:
            model = nodes.CheckpointLoaderSimple().load_checkpoint(relative_model_path)
        except Exception as e:
            print(f"Error loading model with relative path: {e}")
            print(f"Attempting to load from full path: {full_model_path}")
            # Fallback to direct loading if needed
            from comfy.sd import load_checkpoint_guess_config
            model = load_checkpoint_guess_config(full_model_path)

        return (model[0], model[1], model[2], model_info['name'], f"https://civitai.com/models/{model_info['model_id']}")

    @classmethod
    def IS_CHANGED(s, image):
        if image == "none":
            return ""
        image_path = os.path.join(civitai_base_path, image)
        if not os.path.exists(image_path):
            return ""
        
        m = hashlib.sha256()
        with open(image_path, 'rb') as f:
            m.update(f.read())
        m.update(image.encode('utf-8'))
        return m.digest().hex()


class CivitAIModelSelectorPony:
    @classmethod
    def INPUT_TYPES(s):
        # Get list of supported image extensions
        image_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp')
        files = [f"pony/{f}" for f in folder_paths.get_filename_list("pony") 
                if f.lower().endswith(image_extensions)]
        
        if not files:  # If no files found, provide a default option
            files = ["none"]
            
        return {
            "required": {
                        "image": (sorted(files), {"image_upload": True}),
                        "civitai_token": ("STRING", {"default": ""})
            },
        }

    RETURN_TYPES = ("MODEL", "CLIP", "VAE", "STRING", "STRING")
    RETURN_NAMES = ("model", "clip", "vae", "name", "civitai_url")
    FUNCTION = "load_model"
    CATEGORY = "Bjornulf"
 
    def load_model(self, image, civitai_token):
        create_bjornulf_checkpoint_folder()
        if image == "none":
            raise ValueError("No image selected")

        # Get the absolute path to the JSON file
        json_path = os.path.join(parsed_models_path, 'parsed_pony_models.json')

        # Load models info
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                models_info = json.load(f)
        except UnicodeDecodeError:
            # Fallback to latin-1 if UTF-8 fails
            with open(json_path, 'r', encoding='latin-1') as f:
                models_info = json.load(f)
        
        # Extract model name from image path
        image_name = os.path.basename(image)
        # Find corresponding model info
        model_info = next((model for model in models_info 
                          if os.path.basename(model['image_path']) == image_name), None)
        
        if not model_info:
            raise ValueError(f"No model information found for image: {image_name}")

        # Create checkpoints directory if it doesn't exist
        checkpoint_dir = os.path.join(folder_paths.models_dir, "checkpoints", "Bjornulf_civitAI", "pony")
        os.makedirs(checkpoint_dir, exist_ok=True)

        # Expected model filename
        model_filename = f"{model_info['name']}.safetensors"
        full_model_path = os.path.join(checkpoint_dir, model_filename)

        # Check if model is already downloaded
        if not os.path.exists(full_model_path):
            print(f"Downloading model {model_info['name']}...")
            
            # Construct download URL with token
            download_url = model_info['download_url']
            if civitai_token:
                download_url += f"?token={civitai_token}" if '?' not in download_url else f"&token={civitai_token}"

            try:
                # Download the file using class method
                download_file(download_url, checkpoint_dir, model_info['name'], civitai_token)
            except Exception as e:
                raise ValueError(f"Failed to download model: {e}")

        # Get relative path
        relative_model_path = os.path.join("Bjornulf_civitAI", "pony", model_filename)

        # Try loading with relative path first
        try:
            model = nodes.CheckpointLoaderSimple().load_checkpoint(relative_model_path)
        except Exception as e:
            print(f"Error loading model with relative path: {e}")
            print(f"Attempting to load from full path: {full_model_path}")
            # Fallback to direct loading if needed
            from comfy.sd import load_checkpoint_guess_config
            model = load_checkpoint_guess_config(full_model_path)

        return (model[0], model[1], model[2], model_info['name'], f"https://civitai.com/models/{model_info['model_id']}")

    @classmethod
    def IS_CHANGED(s, image, **kwargs):
        if image == "none":
            return ""
        image_path = os.path.join(civitai_base_path, image)
        if not os.path.exists(image_path):
            return ""
        
        m = hashlib.sha256()
        with open(image_path, 'rb') as f:
            m.update(f.read())
        m.update(image.encode('utf-8'))
        return m.digest().hex()

class CivitAILoraSelectorSD15:
    @classmethod
    def INPUT_TYPES(s):
        # Get list of supported image extensions
        image_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp')
        files = [f"lora_sd_1.5/{f}" for f in folder_paths.get_filename_list("lora_sd_1.5") 
                if f.lower().endswith(image_extensions)]
        
        if not files:  # If no files found, provide a default option
            files = ["none"]
            
        return {
            "required": {
                        "image": (sorted(files), {"image_upload": True}),
                        "model": ("MODEL",),
                        "clip": ("CLIP",),
                        "strength_model": ("FLOAT", {"default": 1.0, "min": -20.0, "max": 20.0, "step": 0.01}),
                        "strength_clip": ("FLOAT", {"default": 1.0, "min": -20.0, "max": 20.0, "step": 0.01}),
                        "civitai_token": ("STRING", {"default": ""})
            },
        }
    
    RETURN_TYPES = ("MODEL", "CLIP", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("model", "clip", "name", "civitai_url", "trigger_words")
    FUNCTION = "load_lora"
    CATEGORY = "Bjornulf"
 
    def load_lora(self, image, model, clip, strength_model, strength_clip, civitai_token):
        def download_file(url, destination_path, lora_name, api_token=None):
            """
            Download file with proper authentication headers and simple progress bar.
            """
            filename = f"{lora_name}.safetensors"
            file_path = os.path.join(destination_path, filename)

            headers = {}
            if api_token:
                headers['Authorization'] = f'Bearer {api_token}'

            try:
                print(f"Downloading from: {url}")
                response = requests.get(url, headers=headers, stream=True)
                response.raise_for_status()

                file_size = int(response.headers.get('content-length', 0))
                block_size = 8192
                downloaded = 0

                with open(file_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=block_size):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            
                            if file_size > 0:
                                progress = int(50 * downloaded / file_size)
                                bars = '=' * progress + '-' * (50 - progress)
                                percent = (downloaded / file_size) * 100
                                print(f'\rProgress: [{bars}] {percent:.1f}%', end='')

                print(f"\nFile downloaded successfully to: {file_path}")
                return file_path

            except requests.exceptions.RequestException as e:
                print(f"Error downloading file: {e}")
                raise
        
        if image == "none":
            raise ValueError("No image selected")

        # Get the absolute path to the JSON file
        json_path = os.path.join(parsed_models_path, 'parsed_lora_sd_1.5_loras.json')

        # Load loras info
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                models_info = json.load(f)
        except UnicodeDecodeError:
            # Fallback to latin-1 if UTF-8 fails
            with open(json_path, 'r', encoding='latin-1') as f:
                models_info = json.load(f)
        
        # Extract lora name from image path
        image_name = os.path.basename(image)
        # Find corresponding lora info
        lora_info = next((lora for lora in loras_info 
                         if os.path.basename(lora['image_path']) == image_name), None)
        
        if not lora_info:
            raise ValueError(f"No LoRA information found for image: {image_name}")

        # Create loras directory if it doesn't exist
        lora_dir = os.path.join(folder_paths.models_dir, "loras", "Bjornulf_civitAI", "sd_1.5")
        os.makedirs(lora_dir, exist_ok=True)

        # Expected lora filename
        lora_filename = f"{lora_info['name']}.safetensors"
        full_lora_path = os.path.join(lora_dir, lora_filename)

        # Check if lora is already downloaded
        if not os.path.exists(full_lora_path):
            print(f"Downloading LoRA {lora_info['name']}...")
            
            # Construct download URL with token
            download_url = lora_info['download_url']
            if civitai_token:
                download_url += f"?token={civitai_token}" if '?' not in download_url else f"&token={civitai_token}"

            try:
                # Download the file
                download_file(download_url, lora_dir, lora_info['name'], civitai_token)
            except Exception as e:
                raise ValueError(f"Failed to download LoRA: {e}")

        # Get relative path
        relative_lora_path = os.path.join("Bjornulf_civitAI", "sd_1.5", lora_filename)

        # Load the LoRA
        try:
            lora_loader = nodes.LoraLoader()
            model_lora, clip_lora = lora_loader.load_lora(model=model, 
                                                        clip=clip,
                                                        lora_name=relative_lora_path,
                                                        strength_model=strength_model,
                                                        strength_clip=strength_clip)
        except Exception as e:
            raise ValueError(f"Failed to load LoRA: {e}")

        # Convert trained words list to comma-separated string
        trained_words_str = ", ".join(lora_info.get('trained_words', []))
        
        return (model_lora, clip_lora, lora_info['name'], f"https://civitai.com/models/{lora_info['lora_id']}", trained_words_str)

    @classmethod
    def IS_CHANGED(s, image, **kwargs):
        if image == "none":
            return ""
        image_path = os.path.join(civitai_base_path, image)
        if not os.path.exists(image_path):
            return ""
        
        m = hashlib.sha256()
        with open(image_path, 'rb') as f:
            m.update(f.read())
        m.update(image.encode('utf-8'))
        return m.digest().hex()


class CivitAILoraSelectorSDXL:
    @classmethod
    def INPUT_TYPES(s):
        # Get list of supported image extensions
        image_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp')
        files = [f"lora_sdxl_1.0/{f}" for f in folder_paths.get_filename_list("lora_sdxl_1.0") 
                if f.lower().endswith(image_extensions)]
        
        if not files:  # If no files found, provide a default option
            files = ["none"]
            
        return {
            "required": {
                        "image": (sorted(files), {"image_upload": True}),
                        "model": ("MODEL",),
                        "clip": ("CLIP",),
                        "strength_model": ("FLOAT", {"default": 1.0, "min": -20.0, "max": 20.0, "step": 0.01}),
                        "strength_clip": ("FLOAT", {"default": 1.0, "min": -20.0, "max": 20.0, "step": 0.01}),
                        "civitai_token": ("STRING", {"default": ""})
            },
        }
    
    RETURN_TYPES = ("MODEL", "CLIP", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("model", "clip", "name", "civitai_url", "trigger_words")
    FUNCTION = "load_lora"
    CATEGORY = "Bjornulf"
 
    def load_lora(self, image, model, clip, strength_model, strength_clip, civitai_token):
        def download_file(url, destination_path, lora_name, api_token=None):
            """
            Download file with proper authentication headers and simple progress bar.
            """
            filename = f"{lora_name}.safetensors"
            file_path = os.path.join(destination_path, filename)

            headers = {}
            if api_token:
                headers['Authorization'] = f'Bearer {api_token}'

            try:
                print(f"Downloading from: {url}")
                response = requests.get(url, headers=headers, stream=True)
                response.raise_for_status()

                file_size = int(response.headers.get('content-length', 0))
                block_size = 8192
                downloaded = 0

                with open(file_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=block_size):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            
                            if file_size > 0:
                                progress = int(50 * downloaded / file_size)
                                bars = '=' * progress + '-' * (50 - progress)
                                percent = (downloaded / file_size) * 100
                                print(f'\rProgress: [{bars}] {percent:.1f}%', end='')

                print(f"\nFile downloaded successfully to: {file_path}")
                return file_path

            except requests.exceptions.RequestException as e:
                print(f"Error downloading file: {e}")
                raise
        
        if image == "none":
            raise ValueError("No image selected")

        # Get the absolute path to the JSON file
        json_path = os.path.join(parsed_models_path, 'parsed_lora_sdxl_1.0_loras.json')

        # Load loras info
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                models_info = json.load(f)
        except UnicodeDecodeError:
            # Fallback to latin-1 if UTF-8 fails
            with open(json_path, 'r', encoding='latin-1') as f:
                models_info = json.load(f)
        
        # Extract lora name from image path
        image_name = os.path.basename(image)
        # Find corresponding lora info
        lora_info = next((lora for lora in loras_info 
                         if os.path.basename(lora['image_path']) == image_name), None)
        
        if not lora_info:
            raise ValueError(f"No LoRA information found for image: {image_name}")

        # Create loras directory if it doesn't exist
        lora_dir = os.path.join(folder_paths.models_dir, "loras", "Bjornulf_civitAI", "sdxl_1.0")
        os.makedirs(lora_dir, exist_ok=True)

        # Expected lora filename
        lora_filename = f"{lora_info['name']}.safetensors"
        full_lora_path = os.path.join(lora_dir, lora_filename)

        # Check if lora is already downloaded
        if not os.path.exists(full_lora_path):
            print(f"Downloading LoRA {lora_info['name']}...")
            
            # Construct download URL with token
            download_url = lora_info['download_url']
            if civitai_token:
                download_url += f"?token={civitai_token}" if '?' not in download_url else f"&token={civitai_token}"

            try:
                # Download the file
                download_file(download_url, lora_dir, lora_info['name'], civitai_token)
            except Exception as e:
                raise ValueError(f"Failed to download LoRA: {e}")

        # Get relative path
        relative_lora_path = os.path.join("Bjornulf_civitAI", "sdxl_1.0", lora_filename)

        # Load the LoRA
        try:
            lora_loader = nodes.LoraLoader()
            model_lora, clip_lora = lora_loader.load_lora(model=model, 
                                                        clip=clip,
                                                        lora_name=relative_lora_path,
                                                        strength_model=strength_model,
                                                        strength_clip=strength_clip)
        except Exception as e:
            raise ValueError(f"Failed to load LoRA: {e}")

        # Convert trained words list to comma-separated string
        trained_words_str = ", ".join(lora_info.get('trained_words', []))
        
        return (model_lora, clip_lora, lora_info['name'], f"https://civitai.com/models/{lora_info['lora_id']}", trained_words_str)

    @classmethod
    def IS_CHANGED(s, image, **kwargs):
        if image == "none":
            return ""
        image_path = os.path.join(civitai_base_path, image)
        if not os.path.exists(image_path):
            return ""
        
        m = hashlib.sha256()
        with open(image_path, 'rb') as f:
            m.update(f.read())
        m.update(image.encode('utf-8'))
        return m.digest().hex()


class CivitAILoraSelectorPONY:
    @classmethod
    def INPUT_TYPES(s):
        # Get list of supported image extensions
        image_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp')
        files = [f"lora_pony/{f}" for f in folder_paths.get_filename_list("lora_pony") 
                if f.lower().endswith(image_extensions)]
        
        if not files:  # If no files found, provide a default option
            files = ["none"]
            
        return {
            "required": {
                        "image": (sorted(files), {"image_upload": True}),
                        "model": ("MODEL",),
                        "clip": ("CLIP",),
                        "strength_model": ("FLOAT", {"default": 1.0, "min": -20.0, "max": 20.0, "step": 0.01}),
                        "strength_clip": ("FLOAT", {"default": 1.0, "min": -20.0, "max": 20.0, "step": 0.01}),
                        "civitai_token": ("STRING", {"default": ""})
            },
        }
    
    RETURN_TYPES = ("MODEL", "CLIP", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("model", "clip", "name", "civitai_url", "trigger_words")
    FUNCTION = "load_lora"
    CATEGORY = "Bjornulf"
 
    def load_lora(self, image, model, clip, strength_model, strength_clip, civitai_token):
        def download_file(url, destination_path, lora_name, api_token=None):
            """
            Download file with proper authentication headers and simple progress bar.
            """
            filename = f"{lora_name}.safetensors"
            file_path = os.path.join(destination_path, filename)

            headers = {}
            if api_token:
                headers['Authorization'] = f'Bearer {api_token}'

            try:
                print(f"Downloading from: {url}")
                response = requests.get(url, headers=headers, stream=True)
                response.raise_for_status()

                file_size = int(response.headers.get('content-length', 0))
                block_size = 8192
                downloaded = 0

                with open(file_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=block_size):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            
                            if file_size > 0:
                                progress = int(50 * downloaded / file_size)
                                bars = '=' * progress + '-' * (50 - progress)
                                percent = (downloaded / file_size) * 100
                                print(f'\rProgress: [{bars}] {percent:.1f}%', end='')

                print(f"\nFile downloaded successfully to: {file_path}")
                return file_path

            except requests.exceptions.RequestException as e:
                print(f"Error downloading file: {e}")
                raise
        
        if image == "none":
            raise ValueError("No image selected")

        # Get the absolute path to the JSON file
        json_path = os.path.join(parsed_models_path, 'parsed_lora_pony_loras.json')

        # Load loras info
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                models_info = json.load(f)
        except UnicodeDecodeError:
            # Fallback to latin-1 if UTF-8 fails
            with open(json_path, 'r', encoding='latin-1') as f:
                models_info = json.load(f)
        
        # Extract lora name from image path
        image_name = os.path.basename(image)
        # Find corresponding lora info
        lora_info = next((lora for lora in loras_info 
                         if os.path.basename(lora['image_path']) == image_name), None)
        
        if not lora_info:
            raise ValueError(f"No LoRA information found for image: {image_name}")

        # Create loras directory if it doesn't exist
        lora_dir = os.path.join(folder_paths.models_dir, "loras", "Bjornulf_civitAI", "pony")
        os.makedirs(lora_dir, exist_ok=True)

        # Expected lora filename
        lora_filename = f"{lora_info['name']}.safetensors"
        full_lora_path = os.path.join(lora_dir, lora_filename)

        # Check if lora is already downloaded
        if not os.path.exists(full_lora_path):
            print(f"Downloading LoRA {lora_info['name']}...")
            
            # Construct download URL with token
            download_url = lora_info['download_url']
            if civitai_token:
                download_url += f"?token={civitai_token}" if '?' not in download_url else f"&token={civitai_token}"

            try:
                # Download the file
                download_file(download_url, lora_dir, lora_info['name'], civitai_token)
            except Exception as e:
                raise ValueError(f"Failed to download LoRA: {e}")

        # Get relative path
        relative_lora_path = os.path.join("Bjornulf_civitAI", "pony", lora_filename)

        # Load the LoRA
        try:
            lora_loader = nodes.LoraLoader()
            model_lora, clip_lora = lora_loader.load_lora(model=model, 
                                                        clip=clip,
                                                        lora_name=relative_lora_path,
                                                        strength_model=strength_model,
                                                        strength_clip=strength_clip)
        except Exception as e:
            raise ValueError(f"Failed to load LoRA: {e}")

        # Convert trained words list to comma-separated string
        trained_words_str = ", ".join(lora_info.get('trained_words', []))
        
        return (model_lora, clip_lora, lora_info['name'], f"https://civitai.com/models/{lora_info['lora_id']}", trained_words_str)

    @classmethod
    def IS_CHANGED(s, image, **kwargs):
        if image == "none":
            return ""
        image_path = os.path.join(civitai_base_path, image)
        if not os.path.exists(image_path):
            return ""
        
        m = hashlib.sha256()
        with open(image_path, 'rb') as f:
            m.update(f.read())
        m.update(image.encode('utf-8'))
        return m.digest().hex()



class CivitAILoraSelectorHunyuan:
    @classmethod
    def INPUT_TYPES(s):
        image_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp')
    
        # Try NSFW folder first
        # nsfw_files = [f"NSFW_lora_hunyuan_video/{f}" for f in folder_paths.get_filename_list("NSFW_lora_hunyuan_video") 
        #         if f.lower().endswith(image_extensions)]
        
        # If NSFW folder is empty or doesn't exist, try regular folder
        # if not nsfw_files:
        files = [f"lora_hunyuan_video/{f}" for f in folder_paths.get_filename_list("lora_hunyuan_video") 
            if f.lower().endswith(image_extensions)]
        # else:
        #     files = nsfw_files
        
        if not files:
            files = ["none"]
            
            
        return {
            "required": {
                        "image": (sorted(files), {"image_upload": True}),
                        "model": ("MODEL",),
                        "clip": ("CLIP",),
                        "strength_model": ("FLOAT", {"default": 1.0, "min": -20.0, "max": 20.0, "step": 0.01}),
                        "strength_clip": ("FLOAT", {"default": 1.0, "min": -20.0, "max": 20.0, "step": 0.01}),
                        "civitai_token": ("STRING", {"default": ""})
            },
        }
    
    RETURN_TYPES = ("MODEL", "CLIP", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("model", "clip", "name", "civitai_url", "trigger_words")
    FUNCTION = "load_lora"
    CATEGORY = "Bjornulf"
 
    def load_lora(self, image, model, clip, strength_model, strength_clip, civitai_token):
        def download_file(url, destination_path, lora_name, api_token=None):
            filename = f"{lora_name}.safetensors"
            file_path = os.path.join(destination_path, filename)

            headers = {}
            if api_token:
                headers['Authorization'] = f'Bearer {api_token}'

            try:
                print(f"Downloading from: {url}")
                response = requests.get(url, headers=headers, stream=True)
                response.raise_for_status()

                file_size = int(response.headers.get('content-length', 0))
                block_size = 8192
                downloaded = 0

                with open(file_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=block_size):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            
                            if file_size > 0:
                                progress = int(50 * downloaded / file_size)
                                bars = '=' * progress + '-' * (50 - progress)
                                percent = (downloaded / file_size) * 100
                                print(f'\rProgress: [{bars}] {percent:.1f}%', end='')

                print(f"\nFile downloaded successfully to: {file_path}")
                return file_path

            except requests.exceptions.RequestException as e:
                print(f"Error downloading file: {e}")
                raise
        
        if image == "none":
            raise ValueError("No image selected")

        # Try loading NSFW JSON first, fall back to regular JSON if not found
        # nsfw_json_path = os.path.join(parsed_models_path, 'NSFW_parsed_lora_hunyuan_video_loras.json')
        # regular_json_path = os.path.join(parsed_models_path, 'parsed_lora_hunyuan_video_loras.json')
        json_path = os.path.join(parsed_models_path, 'parsed_lora_hunyuan_video_loras.json')
        
        # json_path = nsfw_json_path if os.path.exists(nsfw_json_path) else regular_json_path
        hunYuan = "hunyuan_video"

        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                models_info = json.load(f)
        except UnicodeDecodeError:
            # Fallback to latin-1 if UTF-8 fails
            with open(json_path, 'r', encoding='latin-1') as f:
                models_info = json.load(f)
        
        image_name = os.path.basename(image)
        lora_info = next((lora for lora in loras_info 
                         if os.path.basename(lora['image_path']) == image_name), None)
        
        if not lora_info:
            raise ValueError(f"No LoRA information found for image: {image_name}")

        lora_dir = os.path.join(folder_paths.models_dir, "loras", "Bjornulf_civitAI", hunYuan)
        os.makedirs(lora_dir, exist_ok=True)

        lora_filename = f"{lora_info['name']}.safetensors"
        full_lora_path = os.path.join(lora_dir, lora_filename)

        if not os.path.exists(full_lora_path):
            print(f"Downloading LoRA {lora_info['name']}...")
            
            download_url = lora_info['download_url']
            if civitai_token:
                download_url += f"?token={civitai_token}" if '?' not in download_url else f"&token={civitai_token}"

            try:
                download_file(download_url, lora_dir, lora_info['name'], civitai_token)
            except Exception as e:
                raise ValueError(f"Failed to download LoRA: {e}")

        relative_lora_path = os.path.join("Bjornulf_civitAI", hunYuan, lora_filename)

        try:
            lora_loader = nodes.LoraLoader()
            model_lora, clip_lora = lora_loader.load_lora(model=model, 
                                                        clip=clip,
                                                        lora_name=relative_lora_path,
                                                        strength_model=strength_model,
                                                        strength_clip=strength_clip)
        except Exception as e:
            raise ValueError(f"Failed to load LoRA: {e}")

        trained_words_str = ", ".join(lora_info.get('trained_words', []))
        
        return (model_lora, clip_lora, lora_info['name'], f"https://civitai.com/models/{lora_info['lora_id']}", trained_words_str)

    @classmethod
    def IS_CHANGED(s, image, **kwargs):
        if image == "none":
            return ""
        image_path = os.path.join(civitai_base_path, image)
        if not os.path.exists(image_path):
            return ""
        
        m = hashlib.sha256()
        with open(image_path, 'rb') as f:
            m.update(f.read())
        m.update(image.encode('utf-8'))
        return m.digest().hex()