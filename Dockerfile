# 默认官方源；镜像站不可达的环境用 --build-arg BASE_IMAGE 覆盖
ARG BASE_IMAGE=ghcr.io/astral-sh/uv:python3.13-bookworm-slim
FROM ${BASE_IMAGE}

# apt 源默认官方；国内可 --build-arg APT_MIRROR=mirrors.cloud.tencent.com
ARG APT_MIRROR=""

# git：wiki 结算经 git 子进程；libgomp：torch；libgl/libglib：MinerU 的 OpenCV 路径
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends git libgomp1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_HTTP_TIMEOUT=600 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

# 先按锁文件装依赖再放源码：源码改动不触发依赖层重建。
# torch 系 wheel 单个可达 3GB，cache mount 让重试构建不重下整层；
# 大 wheel 下载按分钟计，超时给 600 秒；PYPI_INDEX_URL 可指向国内镜像
ARG PYPI_INDEX_URL=https://pypi.org/simple
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project \
        --default-index "$PYPI_INDEX_URL"

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev \
        --default-index "$PYPI_INDEX_URL"

# cl100k_base 构建期预热：运行期不再依赖 tiktoken 的下载地址
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache
RUN mkdir -p "$TIKTOKEN_CACHE_DIR" \
    && .venv/bin/python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" \
    && chmod -R a+rX "$TIKTOKEN_CACHE_DIR"

# uid 1000：与宿主机普通用户对齐，bind mount 的 wiki/workspace 可直接读写
RUN useradd -m -u 1000 wiki
ENV HOME=/home/wiki
USER wiki

# Windows bind mount 的挂载点属主映射为 root，git 属主保护会拒绝操作
RUN git config --global --add safe.directory '*'

COPY --chown=wiki:wiki frontend ./frontend
COPY --chown=wiki:wiki scripts ./scripts

EXPOSE 8000
# project_root 取当前目录（/app）：env/ frontend 从这里解析，数据目录由环境变量指定
CMD ["/app/.venv/bin/uvicorn", "wiki_agent.web:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]
