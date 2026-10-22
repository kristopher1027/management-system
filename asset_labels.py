"""Local QR-code generation for ResourceHub asset labels."""
from io import BytesIO

import qrcode


def make_qr_png(asset_tag):
    """Return a PNG QR code containing a validated asset tag."""
    if not isinstance(asset_tag, str):
        raise ValueError("Asset tag must be text.")
    tag = asset_tag.strip()
    if not tag or len(tag) > 80 or any(ord(c) < 32 for c in tag):
        raise ValueError("Enter a valid asset tag.")

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    qr.add_data(tag)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
