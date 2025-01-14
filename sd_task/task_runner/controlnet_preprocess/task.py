import os
from typing import cast

from PIL import Image

from sd_task import utils
from sd_task.cache import ModelCache
from sd_task.config import Config, get_config
from sd_task.task_args.controlnet_preprocess import \
    ControlnetPreprocessTaskArgs


def run_controlnet_preprocess_task(
    args: ControlnetPreprocessTaskArgs,
    config: Config | None = None,
    model_cache: ModelCache | None = None,
):
    if config is None:
        config = get_config()
    if args.image is not None:
        image = args.image
    elif len(args.image_dataurl) > 0:
        image = utils.decode_image_dataurl(args.image_dataurl)
    else:
        raise ValueError("Image and image_dataurl cannot be both empty")

    args_dict = {}
    preprocess_args = getattr(args.preprocess, "args", None)
    if preprocess_args is not None:
        args_dict = preprocess_args.model_dump()

    resolution = min(image.width, image.height)
    args_dict["detect_resolution"] = resolution
    args_dict["image_resolution"] = resolution

    def load_model():
        from controlnet_aux import processor

        os.environ["HF_HUB_CACHE"] = config.data_dir.models.huggingface
        preprocessor = processor.Processor(args.preprocess.method, args_dict)
        return preprocessor

    if model_cache is not None:
        preprocessor = model_cache.load(args.preprocess.method, load_model)
    else:
        preprocessor = load_model()

    res = preprocessor(image, to_pil=True)
    res = cast(Image.Image, res)
    return res
