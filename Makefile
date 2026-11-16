.PHONY: help install test unit-test lint fmt demo demo-memory eval report smoke clean

PY := .venv/bin/python
EVAL_PY ?= python
EVAL_ARGS ?=
REPORT_RUN ?=
REPORT_ARGS ?=
UNIT_TEST_ARGS ?= -q

help:
	@echo "make install  创建虚拟环境并安装依赖"
	@echo "make test     Run instructor team scenarios (same as make eval)"
	@echo "make unit-test  Run framework pytest regressions without model calls"
	@echo "make lint     代码检查"
	@echo "make fmt      自动修复可修复的问题"
	@echo "make demo     完整链路演示（不需要 API key）"
	@echo "make demo-memory  记忆闭环演示（不需要 API key）"
	@echo "make smoke    验证 DeepSeek key 和模型名"
	@echo "make eval     Run instructor tasks; use EVAL_ARGS=--plan for an offline plan"
	@echo "make report REPORT_RUN=DIR  Rebuild a saved report offline"
	@echo "P3: activate Conda nlp first; EVAL_PY may be an explicit Python path"
	@echo "make clean    清掉运行时产物"

install:
	@test -d .venv || uv venv .venv
	uv pip install -e ".[dev]"

test: eval

unit-test:
	"$(EVAL_PY)" -m pytest $(UNIT_TEST_ARGS)

lint:
	$(PY) -m ruff check src tests

fmt:
	$(PY) -m ruff check src tests --fix

demo:
	$(PY) -m moumou.demo.skeleton

demo-memory:
	$(PY) -m moumou.demo.memory

eval:
	"$(EVAL_PY)" -m moumou.eval.cli --tasks eval/tasks $(EVAL_ARGS)

report:
	"$(EVAL_PY)" -m moumou.eval.report --run "$(REPORT_RUN)" $(REPORT_ARGS)

smoke:
	$(PY) -m moumou.smoke

clean:
	rm -rf runs .pytest_cache .ruff_cache
	find . -name '__pycache__' -type d -not -path './.venv/*' -exec rm -rf {} +
