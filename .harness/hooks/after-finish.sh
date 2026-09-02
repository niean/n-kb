#!/usr/bin/env sh
set -eu

# docker/restart.sh 使用 Bash 专有的 BASH_SOURCE 与 pipefail。
exec bash docker/restart.sh
