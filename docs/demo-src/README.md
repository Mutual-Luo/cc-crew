# 演示视频怎么做的

`demo.html` 是一段按时间线播放的网页动画（卡片和 cc-crew 发到飞书的卡片同一个结构）。`rec.py` 用 Playwright 暂停浏览器时钟、一帧一帧截图，再用 ffmpeg 合成：

```bash
pip install playwright && playwright install chromium
python rec.py 24                     # 截图到 frames/
ffmpeg -framerate 24 -i frames/%05d.png -c:v libx264 -pix_fmt yuv420p -crf 23 -movflags +faststart demo.mp4
ffmpeg -i demo.mp4 -vf "fps=12,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" demo.gif
```

直接用浏览器打开 `demo.html` 也能实时播放。需要 Noto Sans SC、Noto Color Emoji、JetBrains Mono 字体。
