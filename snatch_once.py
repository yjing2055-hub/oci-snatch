#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Oracle Cloud 免费 ARM 实例抢机脚本 —— GitHub Actions 单次尝试版

设计：每次运行只尝试创建一次，立刻退出，由 GitHub Actions 每 5 分钟重新拉起。
这样不需要任何服务器 24 小时挂着，GitHub 的免费 runner 就是重试引擎。

退出码约定（工作流靠它区分情况）：
  0  = 抢到了（或实例已存在且在运行）——工作流会发 Issue 通知并自动停掉自己
  10 = 容量不足 / 被限流 —— 正常情况，静默等下一轮
  1  = 真错误（认证失败、额度上限、找不到子网/镜像）——工作流标红并邮件提醒
  2  = 配置读不到（Secrets 没填好）

规格从环境变量读（GitHub 仓库 Variables 可改，改了不用动代码）：
  OCI_OCPUS      默认 1
  OCI_MEMORY_GB  默认 6
先用 1 核 / 6 GB 占住坑，抢到后在控制台里改成 2 核 / 12 GB（仍在免费额度内）。

只使用甲骨文官方 Python SDK（oci），配置从 ~/.oci/config 读——
工作流会把仓库 Secrets 里的内容写到那里，私钥不经过任何人。
"""

import os
import sys
import time
from datetime import datetime

try:
    import oci
except ImportError:
    print("缺少官方 oci 库（工作流会自动 pip install oci，本地跑请先安装）。")
    sys.exit(2)

from oci.core import ComputeClient, VirtualNetworkClient
from oci.identity import IdentityClient
from oci.core.models import (
    LaunchInstanceDetails,
    CreateVnicDetails,
    InstanceSourceViaImageDetails,
    LaunchInstanceShapeConfigDetails,
)
from oci.exceptions import ServiceError

SHAPE = "VM.Standard.A1.Flex"
DISPLAY_NAME = "a1-nextcloud"
SSH_PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEj0caCa4A54o/ybCeTwJuvT/MbRebGIHOkhsNyC6jFA hatch"

EXIT_OK = 0
EXIT_CAPACITY = 10
EXIT_ERROR = 1
EXIT_CONFIG = 2


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_config():
    try:
        return oci.config.from_file()
    except Exception as e:
        print("读不到 ~/.oci/config：多半是仓库 Secrets 里的 OCI_CONFIG 没填或填错。")
        print(f"（原始错误：{e}）")
        sys.exit(EXIT_CONFIG)


def all_results(fn, *args, **kwargs):
    return oci.pagination.list_call_get_all_results(fn, *args, **kwargs).data


def find_existing_instance(compute, compartment_id):
    try:
        instances = all_results(compute.list_instances, compartment_id)
    except ServiceError:
        return None
    for inst in instances:
        if inst.shape == SHAPE and inst.lifecycle_state not in ("TERMINATED", "TERMINATING"):
            return inst
    return None


def find_public_subnet(vn, compartment_id):
    subnets = [s for s in all_results(vn.list_subnets, compartment_id)
               if s.lifecycle_state == "AVAILABLE"]
    if not subnets:
        return None
    for s in subnets:
        name = s.display_name or ""
        if "public" in name.lower() or "公共" in name:
            return s
    return subnets[0]


def find_ubuntu_image(compute, compartment_id):
    images = all_results(
        compute.list_images,
        compartment_id,
        operating_system="Canonical Ubuntu",
        operating_system_version="24.04",
        shape=SHAPE,
    )
    images = [i for i in images if i.lifecycle_state == "AVAILABLE"]
    if not images:
        return None
    images.sort(key=lambda i: i.time_created, reverse=True)
    return images[0]


def get_public_ip(compute, vn, compartment_id, instance_id):
    atts = all_results(compute.list_vnic_attachments, compartment_id, instance_id=instance_id)
    if not atts:
        return None
    vnic = vn.get_vnic(atts[0].vnic_id).data
    return vnic.public_ip


def wait_until_running(compute, vn, compartment_id, inst):
    """等实例到 RUNNING 并打印成功标记。返回 True=成功（含已创建但开机慢）。"""
    log(f"实例已创建：{inst.display_name}（{inst.lifecycle_state}），等待开机…")
    for _ in range(30):  # 最多等约 10 分钟
        cur = compute.get_instance(inst.id).data
        if cur.lifecycle_state == "RUNNING":
            ip = get_public_ip(compute, vn, compartment_id, cur.id)
            print(f"SNATCH_RESULT=SUCCESS ip={ip}")
            print(f"抢到了！公网 IP：{ip}")
            return True
        if cur.lifecycle_state in ("TERMINATED", "TERMINATING"):
            log("实例被终止了，本轮按未成功处理，下一轮重试。")
            return False
        time.sleep(20)
    print("SNATCH_RESULT=SUCCESS ip=PENDING")
    print("实例已创建，但开机较慢；公网 IP 去 Oracle 控制台里看。")
    return True


def main():
    ocpus = float(os.environ.get("OCI_OCPUS") or 1)
    memory = float(os.environ.get("OCI_MEMORY_GB") or 6)

    config = load_config()
    compartment_id = config["tenancy"]
    compute = ComputeClient(config)
    vn = VirtualNetworkClient(config)
    identity = IdentityClient(config)

    try:
        ads = all_results(identity.list_availability_domains, compartment_id)
        subnet = find_public_subnet(vn, compartment_id)
        image = find_ubuntu_image(compute, compartment_id)
    except ServiceError as e:
        print(f"准备阶段被 API 拒绝（{e.status} {e.code}）：{e.message}")
        print("多半是认证信息不对：检查 Secrets 里 OCI_CONFIG / OCI_PRIVATE_KEY 是否完整粘贴。")
        sys.exit(EXIT_ERROR)

    if subnet is None:
        print("没找到可用子网：去控制台确认 Van-main 及其公共子网还在。")
        sys.exit(EXIT_ERROR)
    if image is None:
        print("没找到 Ubuntu 24.04 的 ARM 镜像。")
        sys.exit(EXIT_ERROR)

    ad_name = ads[0].name
    log(f"可用域 {ad_name}｜子网 {subnet.display_name}｜镜像 {image.display_name}")
    log(f"目标：{SHAPE}，{ocpus:g} OCPU / {memory:g} GB（本轮只试一次）")

    existing = find_existing_instance(compute, compartment_id)
    if existing is not None:
        log(f"发现已有 A1 实例（{existing.lifecycle_state}），不重复创建。")
        ok = wait_until_running(compute, vn, compartment_id, existing)
        sys.exit(EXIT_OK if ok else EXIT_CAPACITY)

    details = LaunchInstanceDetails(
        compartment_id=compartment_id,
        availability_domain=ad_name,
        shape=SHAPE,
        display_name=DISPLAY_NAME,
        shape_config=LaunchInstanceShapeConfigDetails(ocpus=ocpus, memory_in_gbs=memory),
        create_vnic_details=CreateVnicDetails(
            subnet_id=subnet.id, assign_public_ip=True, display_name="a1-vnic"
        ),
        source_details=InstanceSourceViaImageDetails(image_id=image.id),
        metadata={"ssh_authorized_keys": SSH_PUBLIC_KEY},
    )

    try:
        resp = compute.launch_instance(details)
    except ServiceError as e:
        msg = (e.message or "")
        code = (e.code or "")
        if "out of host capacity" in msg.lower():
            print("SNATCH_RESULT=CAPACITY")
            log("容量不足（Out of host capacity），等下一轮。")
            sys.exit(EXIT_CAPACITY)
        if e.status == 429 or code == "TooManyRequests":
            print("SNATCH_RESULT=CAPACITY")
            log("被限流（429），等下一轮。")
            sys.exit(EXIT_CAPACITY)
        if e.status in (401, 403):
            print(f"认证/权限错误（{e.status} {code}）：{msg}")
            print("检查 Secrets 里 OCI_CONFIG / OCI_PRIVATE_KEY 是否完整、是否同一对密钥。")
            sys.exit(EXIT_ERROR)
        if code in ("LimitExceeded", "QuotaExceeded") or "limit" in code.lower():
            print(f"触及免费额度上限（{code}）：{msg}")
            print("多半是已有一台 A1 实例占满了额度，去控制台确认。")
            sys.exit(EXIT_ERROR)
        print(f"其他错误 {e.status} {code}：{msg[:200]}")
        sys.exit(EXIT_ERROR)

    ok = wait_until_running(compute, vn, compartment_id, resp.data)
    sys.exit(EXIT_OK if ok else EXIT_CAPACITY)


if __name__ == "__main__":
    main()
