######################################################################################################
# SPDX-FileCopyrightText: Copyright (c) 2024-2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
######################################################################################################

ARG BASE_IMAGE_URL="nvcr.io/nvidia/clara-holoscan/holoscan"
ARG BASE_IMAGE_TAG="v3.0.0-dgpu"
FROM ${BASE_IMAGE_URL}:${BASE_IMAGE_TAG}

ENV TZ="America/New_York"
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y tzdata

COPY . /workspace/sdr-holoscan

# Install Python dependencies using Poetry
RUN pip install --no-cache-dir poetry && \
    cd /workspace/sdr-holoscan && \
    poetry config virtualenvs.create false && \
    poetry install --no-root

WORKDIR /workspace