"""生成测试用 fixture 图片（程序化生成，无需 git LFS）。"""

from pathlib import Path

from PIL import Image, ImageDraw

FIX = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def make_small() -> Path:
    """256x256 RGB 测试图。"""
    FIX.mkdir(parents=True, exist_ok=True)
    p = FIX / "small_256.png"
    if p.exists():
        return p
    im = Image.new("RGB", (256, 256), (180, 120, 60))
    d = ImageDraw.Draw(im)
    d.rectangle([20, 20, 236, 236], outline=(255, 255, 255), width=3)
    d.text((40, 40), "pixlift", fill=(255, 255, 255))
    d.text((40, 80), "256x256", fill=(255, 255, 255))
    im.save(p)
    return p


def make_big() -> Path:
    """1024x1024 测试图，~3MB。"""
    p = FIX / "big_1024.jpg"
    if p.exists():
        return p
    im = Image.new("RGB", (1024, 1024), (40, 60, 100))
    d = ImageDraw.Draw(im)
    for y in range(0, 1024, 64):
        for x in range(0, 1024, 64):
            c = ((x * 37) % 256, (y * 53) % 256, ((x + y) * 17) % 256)
            d.rectangle([x, y, x + 32, y + 32], fill=c)
    im.save(p, "JPEG", quality=92)
    return p


def main() -> None:
    s = make_small()
    b = make_big()
    print(f"small: {s} ({s.stat().st_size} bytes)")
    print(f"big:   {b} ({b.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
