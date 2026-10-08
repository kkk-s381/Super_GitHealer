# -*- coding: utf-8 -*-
"""
电商订单结算与阶梯营销优惠系统 (Order Pricing & Coupon Service)
包含故意埋入的 3 处典型业务计算缺陷，用于评估测试 GitHealer 智能体的代码自愈能力。
"""

from typing import List, Dict, Optional, Any


def calculate_order_total(
    items: List[Dict[str, Any]],
    coupon: Optional[Dict[str, Any]] = None,
    vip_level: str = "NORMAL"
) -> float:
    """
    计算订单最终实付金额：
    1. 汇总商品基础总额: sum(item['price'] * item['quantity'])
    2. 应用优惠券逻辑:
       - REDUCE (满减券): 订单总额达到门槛 (>= threshold) 时，减免 amount 元
       - PERCENT (折扣券): 订单总额乘以 rate 折扣率 (如 0.85)
       - 规则: 优惠后金额不能低于 0.0 元 (保底)
    3. VIP 会员权益折扣:
       - NORMAL: 无会员折扣
       - VIP: 专享 9 折 (0.9)
       - SVIP: 专享 8 折 (0.8)
    4. 最终实付金额四舍五入保留 2 位小数
    """
    if not items:
        return 0.0

    # 1. 计算商品基础总额
    subtotal = sum(item["price"] * item.get("quantity", 1) for item in items)
    discounted_amount = subtotal

    # 2. 优惠券抵扣
    if coupon:
        coupon_type = coupon.get("type")
        if coupon_type == "REDUCE":
            threshold = float(coupon.get("threshold", 0.0))
            amount = float(coupon.get("amount", 0.0))
            # -----------------------------------------------------------------
            # 🐛 BUG 1 (边界条件缺陷):
            # 错误写成了严格大于 (>)，导致正好达到满减门槛（例如刚好满 200 元）时无法享受立减
            # 正确逻辑应为: if discounted_amount >= threshold:
            # -----------------------------------------------------------------
            if discounted_amount > threshold:
                discounted_amount -= amount
        elif coupon_type == "PERCENT":
            rate = float(coupon.get("rate", 1.0))
            discounted_amount *= rate

    # -----------------------------------------------------------------
    # 🐛 BUG 2 (会员折算逻辑缺陷):
    # VIP 专享 9 折，错误地直接乘以了 0.1，导致实付金额变成了原价的 10%
    # 正确逻辑应为: discounted_amount *= 0.9
    # -----------------------------------------------------------------
    if vip_level == "VIP":
        discounted_amount = discounted_amount * 0.1
    elif vip_level == "SVIP":
        discounted_amount = discounted_amount * 0.8

    # -----------------------------------------------------------------
    # 🐛 BUG 3 (缺少下限防御兜底):
    # 当优惠券抵扣金额超过订单原价时，未做 0 元保底，导致实付金额出现负数
    # 正确逻辑应为: discounted_amount = max(0.0, discounted_amount)
    # -----------------------------------------------------------------
    return round(discounted_amount, 2)
