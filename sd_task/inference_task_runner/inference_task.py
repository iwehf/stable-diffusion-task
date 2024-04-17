import os
import random
from typing import Any, Dict, List

import numpy as np
import torch
from diffusers import (
    AutoencoderKL,
    AutoPipelineForText2Image,
    ControlNetModel,
    DiffusionPipeline,
    DPMSolverMultistepScheduler,
)
from PIL import Image

from sd_task import utils
from sd_task.config import Config, get_config
from sd_task.inference_task_args.controlnet_args import ControlnetArgs
from sd_task.inference_task_args.task_args import (
    InferenceTaskArgs,
    RefinerArgs,
    TaskConfig,
)
from sd_task.cache import ModelCache

from .controlnet import add_controlnet_pipeline_call_args
from .download_model import check_and_prepare_models
from .errors import wrap_download_error, wrap_execution_error
from .log import log
from .prompt import add_prompt_pipeline_call_args, add_prompt_refiner_sdxl_call_args


def get_pipeline_init_args(cache_dir: str, safety_checker: bool = True):
    init_args = {
        "torch_dtype": torch.float16,
        "variant": "fp16",
        "cache_dir": cache_dir,
        "local_files_only": True,
    }

    if not safety_checker is False:
        init_args["safety_checker"] = None

    return init_args


def prepare_pipeline(
    cache_dir: str,
    base_model: str,
    lora_model_name: str = "",
    lora_weight: float = 0,
    controlnet_model_name: str = "",
    vae: str = "",
    refiner_model_name: str = "",
    textual_inversion: str = "",
    safety_checker: bool = True,
):
    pipeline_args = get_pipeline_init_args(cache_dir, safety_checker)
    acc_device = utils.get_accelerator()

    if controlnet_model_name != "":
        controlnet_model = None
        try:
            controlnet_model = ControlNetModel.from_pretrained(
                controlnet_model_name,
                torch_dtype=torch.float16,
                cache_dir=cache_dir,
                variant="fp16",
                local_files_only=True,
            )
        except EnvironmentError:
            pass

        if controlnet_model is None:
            controlnet_model = ControlNetModel.from_pretrained(
                controlnet_model_name,
                torch_dtype=torch.float16,
                cache_dir=cache_dir,
                local_files_only=True,
            )

        pipeline_args["controlnet"] = controlnet_model.to(acc_device)

    pipeline = AutoPipelineForText2Image.from_pretrained(base_model, **pipeline_args)

    # Faster scheduler from the huggingface doc, requires only ~20-25 steps
    pipeline.scheduler = DPMSolverMultistepScheduler.from_config(
        pipeline.scheduler.config
    )

    if vae != "":
        vae_model = None
        try:
            vae_model = AutoencoderKL.from_pretrained(
                vae,
                torch_dtype=torch.float16,
                cache_dir=cache_dir,
                local_files_only=True,
                variant="fp16",
            )
        except EnvironmentError:
            pass

        if vae_model is None:
            vae_model = AutoencoderKL.from_pretrained(
                vae,
                torch_dtype=torch.float16,
                cache_dir=cache_dir,
                local_files_only=True,
            )

        pipeline.vae = vae_model.to(acc_device)

    if lora_model_name != "":
        # raises ValueError if the lora model is not compatible with the base model
        pipeline.load_lora_weights(
            lora_model_name,
            lora_scale=lora_weight,
            cache_dir=cache_dir,
            local_files_only=True,
        )

    if textual_inversion != "":
        pipeline.load_textual_inversion(
            textual_inversion, cache_dir=cache_dir, local_files_only=True
        )

    pipeline = pipeline.to(acc_device)

    # Refiner pipeline
    refiner_model = None

    if refiner_model_name != "":
        refiner_init_args = get_pipeline_init_args(cache_dir)
        refiner_init_args["tokenizer_2"] = pipeline.tokenizer_2
        refiner_init_args["text_encoder_2"] = pipeline.text_encoder_2
        refiner_init_args["vae"] = pipeline.vae
        refiner_model = DiffusionPipeline.from_pretrained(
            refiner_model_name, **refiner_init_args
        ).to(acc_device)

    return pipeline, refiner_model


def get_pipeline_call_args(
    pipeline,
    prompt: str,
    negative_prompt: str,
    task_config: TaskConfig,
    controlnet: ControlnetArgs | None = None,
    refiner: RefinerArgs | None = None,
) -> Dict[str, Any]:
    call_args: Dict[str, Any] = {
        "num_inference_steps": task_config.steps,
        "width": task_config.image_width,
        "height": task_config.image_height,
        "guidance_scale": task_config.cfg,
        # generator on CPU for reproducibility
        "generator": torch.Generator(device="cpu").manual_seed(task_config.seed),
    }

    add_prompt_pipeline_call_args(call_args, pipeline, prompt, negative_prompt)

    if controlnet is not None:
        add_controlnet_pipeline_call_args(
            call_args, controlnet, task_config.image_width, task_config.image_height
        )

    if refiner is not None:
        call_args["output_type"] = "latent"

        # denoising_end is not supported by StableDiffusionXLControlNetPipeline yet.
        if controlnet is None:
            call_args["denoising_end"] = refiner.denoising_cutoff

    return call_args


def run_task(
    args: InferenceTaskArgs,
    config: Config | None = None,
    model_cache: ModelCache | None = None,
) -> List[Image.Image]:
    if config is None:
        config = get_config()

    if utils.get_accelerator() == "cuda":
        # Use deterministic algorithms for reproducibility
        os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":16:8"
        torch.backends.cudnn.benchmark = False
        torch.use_deterministic_algorithms(True)

    log("Task is started")

    torch.manual_seed(args.task_config.seed)
    random.seed(args.task_config.seed)
    np.random.seed(args.task_config.seed)

    with wrap_execution_error():
        model_args: Dict[str, Any] = {
            "base_model": args.base_model,
            "textual_inversion": args.textual_inversion,
            "safety_checker": args.task_config.safety_checker,
        }
        if args.lora is not None:
            model_args["lora_model_name"] = args.lora.model
            model_args["lora_weight"] = args.lora.weight
        if args.controlnet is not None:
            model_args["controlnet_model_name"] = args.controlnet.model
        if args.vae != "":
            model_args["vae"] = args.vae
        if args.refiner is not None:
            model_args["refiner_model_name"] = args.refiner.model

        if model_cache is not None and model_cache.has(model_args):
            pipeline, refiner = model_cache.get(model_args)
        else:
            log("Check the model cache and download the models")

            with wrap_download_error():
                check_and_prepare_models(
                    args,
                    external_model_cache_dir=config.data_dir.models.external,
                    hf_model_cache_dir=config.data_dir.models.huggingface,
                    proxy=config.proxy,
                )

            log("All the required models are downloaded")

            pipeline, refiner = prepare_pipeline(
                cache_dir=config.data_dir.models.huggingface, **model_args
            )
            if model_cache is not None:
                model_cache.set(model_args, (pipeline, refiner))
            log("The pipeline has been successfully loaded")

        generated_images = []

        call_args = get_pipeline_call_args(
            pipeline,
            args.prompt,
            args.negative_prompt,
            args.task_config,
            args.controlnet,
            args.refiner,
        )

        refiner_call_args = {"generator": call_args["generator"]}

        if args.refiner is not None and refiner is not None:
            add_prompt_refiner_sdxl_call_args(
                refiner_call_args, refiner, args.prompt, args.negative_prompt
            )
            refiner_call_args["num_inference_steps"] = args.refiner.steps

            # denoising_end is not supported by StableDiffusionXLControlNetPipeline yet.
            if args.controlnet is None:
                refiner_call_args["denoising_start"] = args.refiner.denoising_cutoff


        log("The images generation is started")
        for _ in range(args.task_config.num_images):
            image = pipeline(**call_args)

            if refiner is not None:
                refiner_call_args["image"] = image.images
                image = refiner(**refiner_call_args)

            generated_images.append(image.images[0])

        log("The images generation is finished")

        return generated_images
