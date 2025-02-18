#!/bin/bash

# If this looks stupid, that's because it is.
# The issue is that selenium and/or chromedriver leave around zombie procs.
# However, this issue magically goes away if bash owns the python process.
# I suspect this has something to do with the entrypoint proc getting pid 1,
# which is usually reserved for the kernel (init).

# export DISPLAY=:0

# Xvfb $DISPLAY -screen 0 1920x1080x24+32 &

/src/worker/watchdog.bash python /src/worker/lean_and_mean.py &

python /src/run.py $@
