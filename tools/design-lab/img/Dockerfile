FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends fonts-noto-cjk fonts-nanum fonts-inter >/dev/null 2>&1 && fc-cache -f >/dev/null 2>&1
RUN pip install -q --break-system-packages playwright==1.63.0 2>&1 | tail -2
