"""Input image inspection helpers shared by ComfyUI preflight and workflows."""

EXIF_ORIENTATION = 274


def inspect_image(path, *, single_frame=True):
    from PIL import Image

    try:
        with Image.open(path) as image:
            if single_frame and getattr(image, "n_frames", 1) != 1:
                raise ValueError("只接受单帧图片")
            width, height = image.size
            # EXIF transpose orientations 5–8 swap dimensions without decoding pixels here.
            if image.getexif().get(EXIF_ORIENTATION) in (5, 6, 7, 8):
                width, height = height, width
            return {"width": width, "height": height}
    except (OSError, Image.DecompressionBombError, SyntaxError) as error:
        raise ValueError("无法读取单帧图片") from error
