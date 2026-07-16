#!/usr/bin/env python3
import base64, sys

src = sys.argv[1]
dst = sys.argv[2]
with open(src, "r") as f:
    b64 = f.read().strip()
with open(dst, "wb") as f:
    f.write(base64.b64decode(b64))
