"""Small valid PDF bytes for source-reader tests, with optional real encryption."""

import io

from pypdf import PdfWriter


def pdf_bytes(*, encrypted=False, user_password="test-password", algorithm="AES-256"):
    with PdfWriter() as writer:
        writer.add_blank_page(width=72, height=72)
        if encrypted:
            writer.encrypt(user_password, owner_password="test-owner-password", algorithm=algorithm)
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()
