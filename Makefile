.PHONY: help install test lint fmt demo demo-memory eval smoke clean

PY := .venv/bin/python

help:
	@echo "make install  创建虚拟环境并安装依赖"
	@echo "make test     跑全部测试"
	@echo "make lint     代码检查"
	@echo "make fmt      自动修复可修复的问题"
	@echo "make demo     完整链路演示（不需要 API key）"
	@echo "make demo-memory  记忆闭环演示（不需要 API key）"
	@echo "make smoke    验证 DeepSeek key 和模型名"
	@echo "make eval     跑评测集（需要 API key）"
	@echo "make clean    清掉运行时产物"

install:
	@test -d .venv || uv venv .venv
	uv pip install -e ".[dev]"

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check src tests

fmt:
	$(PY) -m ruff check src tests --fix

demo:
	$(PY) -m moumou.demo.skeleton

demo-memory:
	$(PY) -m moumou.demo.memory

eval:
	$(PY) -m moumou.eval.cli --tasks eval/tasks

smoke:
	$(PY) -m moumou.smoke

clean:
	rm -rf runs .pytest_cache .ruff_cache
	find . -name '__pycache__' -type d -not -path './.venv/*' -exec rm -rf {} +
