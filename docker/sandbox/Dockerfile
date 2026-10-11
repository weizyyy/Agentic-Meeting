FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="Agentic-Meeting code sandbox" \
      org.opencontainers.image.description="Python with numpy, pandas and matplotlib for the Agentic-Meeting background agent" \
      org.opencontainers.image.source="https://github.com/weizyyy/Agentic-Meeting" \
      org.opencontainers.image.licenses="MIT"

# 一个小的中文字体，否则图表里的中文会显示成方块
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-wqy-microhei \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir numpy pandas matplotlib

# 无显示环境下画图；默认使用中文字体
ENV MPLBACKEND=Agg
RUN mkdir -p /root/.config/matplotlib \
    && printf 'font.sans-serif: WenQuanYi Micro Hei, DejaVu Sans\naxes.unicode_minus: False\n' \
       > /root/.config/matplotlib/matplotlibrc

WORKDIR /workspace
