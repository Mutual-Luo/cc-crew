"""把 demo.html 一帧一帧拍下来：假时钟每次往前推 1/fps 秒，截图存 frames/。用法：rec.py fps [只拍这些秒,逗号分隔]"""
import os, sys
from playwright.sync_api import sync_playwright
HERE = os.path.dirname(os.path.abspath(__file__))
fps = float(sys.argv[1]) if len(sys.argv) > 1 else 15
only = [float(x) for x in sys.argv[2].split(",")] if len(sys.argv) > 2 else None
scale = float(os.environ.get("SCALE", "1.5"))
out = os.path.join(HERE, "frames" if not only else "stills")
os.makedirs(out, exist_ok=True)
for f in os.listdir(out):
    os.remove(os.path.join(out, f))
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1280, "height": 720}, device_scale_factor=scale)
    pg.clock.install(time=0)
    pg.clock.pause_at(1)                    # 时间不自己走：只靠下面 run_for 一格一格推，截图再慢也不会跳帧
    pg.goto("file://" + os.path.join(HERE, "demo.html"))
    dur = pg.evaluate("window.DURATION")
    t, n, step = 0.0, 0, 1000.0 / fps
    targets = sorted(only) if only else None
    while t < dur * 1000:
        t += step
        now = pg.evaluate("Date.now()")
        if t - now >= 1:
            pg.clock.run_for(int(round(t - now)))
        elif n == 0 and targets is None and t < 50:
            pass
        if targets is None:
            pg.screenshot(path=os.path.join(out, f"{n:05d}.png")); n += 1
        elif targets and t >= targets[0] * 1000:
            pg.screenshot(path=os.path.join(out, f"t{targets.pop(0):05.1f}.png"))
            if not targets: break
    b.close()
print("frames:", len(os.listdir(out)))
