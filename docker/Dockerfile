FROM python:3.11.15-slim-trixie@sha256:f9fa7f851e38bfb19c9de3afbc4b86ae7176ea7aaf94535c31df5458d5849457

WORKDIR /app

COPY pyproject.toml /app/pyproject.toml
COPY app /app/app

#RUN pip install --no-cache-dir .
# py-spy 固定 0.4.2：N-Agent T9 S1（2026-10-24）已核验该版本与
# grafana/pyroscope:1.21.1 的 /ingest folded 契约兼容；PyPI 提供面向
# manylinux2014 aarch64/x86_64 的 py2.py3 通用 wheel，与 slim-trixie 兼容。
# 构建期执行 py-spy --version，确保镜像内二进制可用；普通部署只安装不开启采样。
RUN pip install . py-spy==0.4.2 \
    && py-spy --version

EXPOSE 8212

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8212"]
