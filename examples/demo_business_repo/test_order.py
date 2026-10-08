# -*- coding: utf-8 -*-
"""
电商订单结算单测套件 (Order Pricing Test Suite)
用于验证业务逻辑完整性与缺陷复现。
"""

import sys
import os

# 确保导入同目录下的 order_service
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from order_service import calculate_order_total


def test_standard_order():
    """用例 1: 普通用户标准订单（无优惠券，正常计费）"""
    items = [{"price": 49.9, "quantity": 2}, {"price": 100.2, "quantity": 1}]
    # 49.9 * 2 + 100.2 = 200.0
    total = calculate_order_total(items, coupon=None, vip_level="NORMAL")
    assert total == 200.0, f"普通订单计费错误，期望 200.0，实际得到 {total}"


def test_vip_discount():
    """用例 2: VIP 会员享受 9 折 (0.9) 优惠"""
    items = [{"price": 100.0, "quantity": 1}]
    # VIP 打 9 折后应付 90.0 元
    total = calculate_order_total(items, coupon=None, vip_level="VIP")
    assert total == 90.0, f"VIP 9折计算错误，100元订单期望 90.0，实际得到 {total}"


def test_coupon_boundary_threshold():
    """用例 3: 满减券门槛临界值测试（刚好满 200 元减 30 元）"""
    items = [{"price": 100.0, "quantity": 2}]  # 总额正好 200.0 元
    coupon = {"type": "REDUCE", "threshold": 200.0, "amount": 30.0}
    # 达到 200 元满减门槛，应减免 30 元，实付 170.0 元
    total = calculate_order_total(items, coupon=coupon, vip_level="NORMAL")
    assert total == 170.0, f"满减门槛临界值校验失败，满200减30期望 170.0，实际得到 {total}"


def test_coupon_over_deduction_protection():
    """用例 4: 优惠券抵扣超限保底（抵扣额大于商品总额，不能出现负数）"""
    items = [{"price": 20.0, "quantity": 1}]  # 总额 20 元
    coupon = {"type": "REDUCE", "threshold": 10.0, "amount": 50.0}
    # 20 元扣减 50 元，实付金额保底为 0.0 元
    total = calculate_order_total(items, coupon=coupon, vip_level="NORMAL")
    assert total == 0.0, f"优惠金额超限保底校验失败，期望保底 0.0，实际得到 {total}"


if __name__ == "__main__":
    test_cases = [
        test_standard_order,
        test_vip_discount,
        test_coupon_boundary_threshold,
        test_coupon_over_deduction_protection,
    ]

    failed_count = 0
    print("=" * 60)
    print("  [TEST] 启动电商订单计费系统回归测试套件")
    print("=" * 60)

    for test_fn in test_cases:
        test_name = test_fn.__name__
        try:
            test_fn()
            print(f"  [PASS] {test_name}")
        except AssertionError as err:
            print(f"  [FAIL] {test_name} -> {err}")
            failed_count += 1
        except Exception as err:
            print(f"  [ERROR] {test_name} 运行抛出意外异常 -> {err}")
            failed_count += 1

    print("=" * 60)
    if failed_count > 0:
        print(f"  [FAILED] 汇总: 共 {failed_count} 个测试用例未通过！单测未通过，请修复代码缺陷。")
        print("=" * 60)
        sys.exit(1)
    else:
        print("  [PASSED] 汇总: 全部测试用例验证通过！")
        print("=" * 60)
        sys.exit(0)
