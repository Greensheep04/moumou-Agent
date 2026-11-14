.PHONY: help install test lint fmt demo demo-memory eval report ablation ablation-plan smoke clean

PY := .venv/bin/python
EVAL_PY ?= python
EVAL_ARGS ?=
REPORT_RUN ?=
REPORT_ARGS ?=
ABLATION_ARGS ?= --plan

help:
	@echo "make install  创建虚拟环境并安装依赖"
	@echo "make test     跑全部测试"
	@echo "make lint     代码检查"
	@echo "make fmt      自动修复可修复的问题"
	@echo "make demo     完整链路演示（不需要 API key）"
	@echo "make demo-memory  记忆闭环演示（不需要 API key）"
	@echo "make smoke    验证 DeepSeek key 和模型名"
	@echo "make eval     跑评测集（需要 API key）"
	@echo "make report REPORT_RUN=DIR  Rebuild a saved report offline"
	@echo "make ablation Default: API-free dry plan; override ABLATION_ARGS to execute"
	@echo "make ablation-plan  Print the four-group plan without API calls"
	@echo "P3: activate Conda nlp first; EVAL_PY may be an explicit Python path"
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
	"$(EVAL_PY)" -m moumou.eval.cli --tasks eval/tasks $(EVAL_ARGS)

report:
	"$(EVAL_PY)" -m moumou.eval.report --run "$(REPORT_RUN)" $(REPORT_ARGS)

ablation:
	"$(EVAL_PY)" -m moumou.eval.ablation $(ABLATION_ARGS)

ablation-plan:
	"$(EVAL_PY)" -m moumou.eval.ablation --plan $(filter-out --plan,$(ABLATION_ARGS))

smoke:
	$(PY) -m moumou.smoke

clean:
	rm -rf runs .pytest_cache .ruff_cache
	find . -name '__pycache__' -type d -not -path './.venv/*' -exec rm -rf {} +
