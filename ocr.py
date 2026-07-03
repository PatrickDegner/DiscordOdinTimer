import os
import pytesseract
from PIL import Image, ImageGrab
import cv2
import numpy as np
import re
import time

# Configure Tesseract executable path from environment if provided
tess_path = os.getenv('TESSERACT_PATH')
if tess_path:
    pytesseract.pytesseract.tesseract_cmd = tess_path


def preprocess_image(image: Image.Image) -> Image.Image:
    """
    Applies image processing steps to improve OCR accuracy.
    Upscales, converts to grayscale, and applies Otsu's binarization.
    """
    img_upscaled = image.resize((image.width * 4, image.height * 4), Image.LANCZOS)

    img_np = np.array(img_upscaled)

    if len(img_np.shape) == 3 and img_np.shape[2] == 4:  # RGBA
        img_gray = cv2.cvtColor(img_np, cv2.COLOR_RGBA2GRAY)
    elif len(img_np.shape) == 3 and img_np.shape[2] == 3:  # RGB
        img_gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)
    else:
        img_gray = img_np

    _, img_bw = cv2.threshold(img_gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    img_pil_bw = Image.fromarray(img_bw)
    return img_pil_bw


def parse_boss_info(image_source):
    """
    Processes an image, runs OCR on it, and attempts to extract boss name
    and remaining time using a regular expression.
    Returns: (discord_message, future_timestamp, boss_name)
    """
    try:
        if not isinstance(image_source, Image.Image):
            return "ERROR: Invalid image source provided. Expected a PIL Image object.", None, None

        img = image_source
        processed_img = preprocess_image(img)

        custom_config = r'--oem 1 --psm 6 -l eng'
        full_text = pytesseract.image_to_string(processed_img, config=custom_config)

        # Small post-processing for common OCR errors
        full_text = full_text.replace("(S ih", "(S 1h").replace("(S Ih", "(S 1h").replace("(S th", "(S 1h")
        full_text = full_text.replace("(SS ih", "(SS 1h").replace("(SS Ih", "(SS 1h").replace("(SS th", "(SS 1h")
        full_text = full_text.replace("S ih", "S 1h").replace("S Ih", "S 1h").replace("S th", "S 1h")
        full_text = full_text.replace("SS ih", "SS 1h").replace("SS Ih", "SS 1h").replace("SS th", "SS 1h")
        full_text = full_text.replace("© th", "© 1hh").replace("© ih", "© 1h").replace("© Ih", "© 1h")

        if not full_text.strip():
            error_msg = (
                "ERROR: No text was extracted from the image by Tesseract OCR."
            )
            return error_msg, None, None

        boss_pattern = re.compile(
            r"Domain Ruler\s+([A-Za-z\s-]+?)(?=\s*\d+h|\s*\d+m|\s*\d+s|[\r\n])[\s\S]*?"
            r"(?:[\D]*?(\d+)\s*h)?"
            r"(?:[\D]*?(\d+)\s*m)?"
            r"(?:\s*[\D]*?(\d+)\s*[sSeCgG])?"
            r"[\s\S]*?"
            r"[lLeEfF]{3,4}",
            re.IGNORECASE | re.DOTALL
        )

        match = boss_pattern.search(full_text)

        if match:
            boss_name = re.sub(r'\s+', ' ', match.group(1)).strip()
            hours = int(match.group(2)) if match.group(2) else 0
            minutes = int(match.group(3)) if match.group(3) else 0
            seconds = int(match.group(4)) if match.group(4) else 0

            total_seconds_remaining = (hours * 3600) + (minutes * 60) + seconds
            future_timestamp = int(time.time() + total_seconds_remaining)

            discord_message = (
                f"**{boss_name}** \n<t:{future_timestamp}:F> thats <t:{future_timestamp}:R>"
            )
            return discord_message, future_timestamp, boss_name
        else:
            return "Could not find any 'Domain Ruler' boss with remaining time (hours/minutes/seconds) in the extracted text.", None, None

    except pytesseract.TesseractNotFoundError:
        return (
            "ERROR: Tesseract OCR engine not found. Please install Tesseract and configure its path if necessary."
        ), None, None
    except Exception as e:
        return f"An unexpected error occurred: {e}", None, None


if __name__ == "__main__":
    clipboard_image = ImageGrab.grabclipboard()
    if clipboard_image:
        print("Processing image from clipboard...")
        result_message, _, _ = parse_boss_info(clipboard_image)
    else:
        result_message = "ERROR: No image found in clipboard."
    print("\n--- OCR Result ---")
    print(result_message)
    print("----------------------------")
