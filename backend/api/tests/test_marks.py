from app.services.marks import detect_marks
from tests.imaging import add_seal, add_signature, font, page

TEXT = ["Certificate of residence No. 12345", "Issued to the student for submission", "to the trade union committee"]


def test_plain_text_has_no_marks():
    img, _ = page(lines=TEXT)
    marks = detect_marks(img)
    assert (marks.seal, marks.signature) == (False, False)


def test_blue_seal_and_signature_detected():
    img, draw = page(lines=TEXT)
    add_seal(draw, 420, 1300)
    add_signature(draw, 700, 1300)
    marks = detect_marks(img)
    assert marks.seal and marks.signature


def test_only_seal():
    img, draw = page(lines=TEXT)
    add_seal(draw, 600, 1200)
    marks = detect_marks(img)
    assert marks.seal and not marks.signature


def test_only_signature():
    img, draw = page(lines=TEXT)
    add_signature(draw, 500, 1300)
    marks = detect_marks(img)
    assert marks.signature and not marks.seal


def test_red_seal():
    img, draw = page(lines=TEXT)
    add_seal(draw, 600, 1200, color=(200, 30, 30))
    assert detect_marks(img).seal


def test_black_and_white_scan_seal():
    img, draw = page(lines=TEXT, grayscale=True)
    add_seal(draw, 600, 1200, color=0)
    marks = detect_marks(img)
    assert marks.seal


def test_blue_printed_text_is_not_a_signature():
    img, draw = page(lines=TEXT)
    draw.text((200, 1300), "Blue printed caption of the document", fill=(25, 55, 190), font=font(34))
    marks = detect_marks(img)
    assert not marks.signature and not marks.seal
