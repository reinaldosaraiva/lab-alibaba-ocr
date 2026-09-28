REENTRY_CHECKER ?= $(HOME)/.claude/skills/reentry/scripts/check_plans_consistency.sh
REVIEW_HOME     ?= $(HOME)/workspace/qwen-coder-quality/review-model
TOOL            ?= semgrep
BENCH_OUT       ?= results/p001-s003-benchmark/floor.json
V               ?= adapter
I               ?= unseen
LAB_ENV      := .lab/gitlab.env
GITLAB_URL   := http://localhost:8929

.PHONY: check pre-commit checker baseline-hashes bench-floor s0a-batch s0a-report help lab-up lab-tools lab-ocr lab-gitlab-up lab-l1-check lab-manifest lab-down lab-nuke

check: pre-commit checker

pre-commit:
	pre-commit run --all-files

checker:
	bash $(REENTRY_CHECKER) .

baseline-hashes:
	@printf 'open-code-review %s\n' "$$(git -C open-code-review rev-parse --short HEAD)"
	@printf 'review-model     %s\n' "$$(git -C $(REVIEW_HOME) rev-parse --short HEAD)"
	@shasum -a 256 $(REVIEW_HOME)/adapters/review/adapters.safetensors $(REVIEW_HOME)/data/eval/gold.all.jsonl

# $(REVIEW_HOME) vem do make porque o hook do host nega shell com caminho
# cross-root literal (mesmo padrão de lab-l1-check; DR5 item 22).
bench-floor:
	python3 scripts/benchmark/run_floor.py --tool $(TOOL) \
		--gold $(REVIEW_HOME)/data/eval/gold.all.jsonl \
		--unseen $(REVIEW_HOME)/results/gold.unseen.jsonl \
		--out $(BENCH_OUT)

# Spike S0-A (P001-S005): mesma razão do $(REVIEW_HOME) do bench-floor, e o
# runner usa o .venv-train do review-model (Python 3.12 + mlx_lm). V/I
# selecionam a célula da matriz {base,adapter} × {unseen,mrs50}
# (ex.: make s0a-batch V=base I=mrs50). s0a-report exige a matriz completa.
s0a-batch:
	$(REVIEW_HOME)/.venv-train/bin/python scripts/spike/s0a_batch.py \
		--variant $(V) --inputs $(I) --review-home $(REVIEW_HOME)

s0a-report:
	$(REVIEW_HOME)/.venv-train/bin/python scripts/spike/s0a_report.py \
		--review-home $(REVIEW_HOME)

help:
	@grep -E '^[a-z-]+:' Makefile | cut -d: -f1

lab-up: lab-tools lab-ocr lab-gitlab-up lab-l1-check lab-manifest

lab-tools:
	python3 scripts/lab/lab_tools.py

lab-ocr:
	@set -e; \
	commit="$$(git -C open-code-review rev-parse --short HEAD)"; \
	if [ -x bin/ocr ] && [ "$$(cat .lab/ocr-commit 2>/dev/null)" = "$$commit" ]; then \
		echo "lab-ocr: up to date ($$commit)"; \
	else \
		echo "lab-ocr: building open-code-review at $$commit"; \
		if make -C open-code-review build; then \
			mkdir -p bin .lab; \
			ln -sf ../open-code-review/dist/opencodereview bin/ocr; \
			printf '%s\n' "$$commit" > .lab/ocr-commit; \
			printf 'build-from-source\n' > .lab/ocr-install; \
			echo "lab-ocr: built from source at $$commit"; \
		else \
			echo "lab-ocr: source build failed; installing npm fallback 1.12.8"; \
			rm -f bin/ocr .lab/ocr-commit; \
			npm install -g @alibaba-group/open-code-review@1.12.8; \
			mkdir -p .lab; \
			printf 'npm-fallback:1.12.8\n' > .lab/ocr-install; \
			printf 'make -C open-code-review build failed at %s\n' "$$commit" \
				> .lab/ocr-fallback-reason; \
			echo "lab-ocr: npm fallback installed (1.12.8)"; \
		fi; \
	fi

lab-gitlab-up:
	@if [ ! -f $(LAB_ENV) ]; then \
		mkdir -p .lab; \
		printf 'GITLAB_ROOT_PASSWORD=%s\n' "$$(openssl rand -hex 16)" > $(LAB_ENV); \
		chmod 600 $(LAB_ENV); \
		echo "lab-gitlab-up: generated $(LAB_ENV) (root password randomized)"; \
	fi
	@set -a; . ./.lab/gitlab.env; set +a; \
	docker compose -f lab/gitlab/compose.yaml up -d
	@set -a; . ./.lab/gitlab.env; set +a; \
	python3 scripts/lab/gitlab_bootstrap.py --url $(GITLAB_URL) \
		--root-password-env GITLAB_ROOT_PASSWORD --out .lab/gitlab.json \
		--container gitlab-gitlab-1

lab-l1-check:
	python3 scripts/lab/l1_check.py --review-home $(REVIEW_HOME)

lab-manifest:
	python3 scripts/lab/lab_manifest.py --review-home $(REVIEW_HOME)

lab-down:
	@set -a; . ./.lab/gitlab.env 2>/dev/null; set +a; \
	docker compose -f lab/gitlab/compose.yaml down

lab-nuke:
	-set -a; . ./.lab/gitlab.env 2>/dev/null; set +a; \
	python3 scripts/lab/gitlab_bootstrap.py --url $(GITLAB_URL) \
		--root-password-env GITLAB_ROOT_PASSWORD --out .lab/gitlab.json --revoke \
		--container gitlab-gitlab-1
	@set -a; . ./.lab/gitlab.env 2>/dev/null; set +a; \
	docker compose -f lab/gitlab/compose.yaml down -v --remove-orphans
	rm -rf .lab
