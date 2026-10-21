"""随代码发布的公共策略目录。

上游去内置化后只扫 ``data/strategies/*``, 本仓库仍把公共策略以源码形式放在
此目录发布, 由 ``app.main`` 与 ``app.backtest.worker`` 的 ``strategy_dirs``
扫描加载, 二者必须保持一致。

``factor_rank_research`` 只是转发到 ``app.strategy.research`` 的兼容垫片,
置于同名子包以避开扫描: 它 ``import sys`` 会被策略安全校验拒绝, 直接扫描还会
与 research 下的真实定义撞 id。旧版用户策略文件
``from app.strategy.builtin.factor_rank_research import ...`` 仍正常工作。
"""
