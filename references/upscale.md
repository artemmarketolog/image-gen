# Optional local upscale

Install extra dependencies only when needed:

```bash
.venv/bin/python -m pip install -r requirements-upscale.txt
.venv/bin/python upscale.py --input original.png --output enlarged.png --size 2160x3840
```

The first run downloads the public Real-ESRGAN x4plus weights from the project's GitHub release
into this user's cache. PyTorch adds a substantial download; CPU rendering can take many minutes.
The input remains untouched. Compare faces and fine lettering: neural upscaling estimates new detail and is not lossless.
Real-ESRGAN is a separate third-party project: https://github.com/xinntao/Real-ESRGAN.
