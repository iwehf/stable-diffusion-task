from sd_task.task_runner import run_inference_task
from sd_task.task_args import InferenceTaskArgs
from diffusers.utils import make_image_grid
from reference_image import get_controlnet_ref_image_dataurl


if __name__ == '__main__':
    ref_image = get_controlnet_ref_image_dataurl()

    prompt = ("best quality, ultra high res, photorealistic++++, 1girl, off-shoulder sweater, smiling, "
              "faded ash gray messy bun hair+, border light, depth of field, looking at "
              "viewer, closeup")

    negative_prompt = ("paintings, sketches, worst quality+++++, low quality+++++, normal quality+++++, lowres, "
                       "normal quality, monochrome++, grayscale++, skin spots, acnes, skin blemishes, "
                       "age spot, glans")

    args = {
        "version": "2.0.0",
        "base_model": {
            "name": "emilianJR/chilloutmix_NiPrunedFp32Fix",
            "variant": None
        },
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "task_config": {
            "num_images": 9,
            "safety_checker": False,
            "steps": 40
        },
        "lora": {
            "model": "https://civitai.com/api/download/models/31284",
            "weight": 70
        },
        "controlnet": {
            "model": "lllyasviel/control_v11p_sd15_openpose",
            "weight": 90,
            "image_dataurl": ref_image,
            "preprocess": {
                "method": "openpose_face"
            }
        }
    }

    images = run_inference_task(InferenceTaskArgs.model_validate(args))
    image_grid = make_image_grid(images, 3, 3)
    image_grid.save("./data/sd15_lora_controlnet.png")
