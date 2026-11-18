# Every target mirrors a CI job: green here = green in CI.
#   make all     lint + test + manifests + security + docs   (no cluster, ~1 min)
#   make e2e     kind + Moto + Lowkey Vault + the real image  (Docker, ~10 min)
SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c
UV ?= uv
HUB_RAW := https://raw.githubusercontent.com/ngems1/code-clinic/v1
IMAGE := ghcr.io/ngems1/code-clinic-devcontainer:1.1.0
DOCKER_E2E := docker run --rm --user root -v /var/run/docker.sock:/var/run/docker.sock -v "$(CURDIR):/work" -w /work

.PHONY: help setup lint test manifests image security docs e2e e2e-up e2e-down ci-local all

help:      ## list targets
	@grep -E '^[a-z0-9-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

setup:     ## install dev dependencies and git hooks
	$(UV) sync --locked
	pre-commit install

lint:      ## ruff, mypy, shellcheck, hadolint, actionlint (CI: python, shell, image, repo)
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy
	shellcheck tests/e2e/*.sh
	hadolint Dockerfile
	actionlint

test:      ## unit tests: spec, policy, providers (Moto), reconcile, operator (CI: python)
	$(UV) run pytest

manifests: ## kustomize build + kubeconform for deploy/ and the e2e overlay (CI: manifests)
	@for d in deploy tests/e2e/overlay; do \
	  kustomize build "$$d" | kubeconform -strict -summary -kubernetes-version 1.37.0 | sed "s#^#$$d: #"; done
	kubeconform -strict -summary deploy/tenant-rolebinding.yaml

image:     ## build the operator image and scan it with Trivy (CI: image)
	docker build -t secret-sync:dev .
	trivy image --quiet --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 secret-sync:dev

security:  ## gitleaks: files + this branch's history (CI: repo)
	gitleaks git --redact --no-banner --log-opts=HEAD .
	gitleaks dir --redact --no-banner .

docs:      ## course self-check: README structure, links, <details>, SVG, encoding (CI: repo)
	@if [ -f ../code-clinic/scripts/check_repo.py ]; then \
	  python3 ../code-clinic/scripts/check_repo.py --profile project; \
	else \
	  tmp="$$(mktemp)"; trap 'rm -f "$$tmp"' EXIT; \
	  curl -fsSL "$(HUB_RAW)/scripts/check_repo.py" -o "$$tmp"; \
	  python3 "$$tmp" --profile project; \
	fi

e2e:       ## kind cluster, fakes, operator: every scenario; deleted afterwards (CI: e2e)
	$(DOCKER_E2E) $(IMAGE) bash tests/e2e/run.sh

e2e-up:    ## same, but KEEP the cluster (KUBECONFIG=.e2e/kubeconfig) for the drills
	$(DOCKER_E2E) -e KEEP=1 $(IMAGE) bash tests/e2e/run.sh

e2e-down:  ## delete the e2e cluster
	$(DOCKER_E2E) $(IMAGE) kind delete cluster --name secret-sync-e2e

ci-local:  ## run .github/workflows/ci.yml with act on a clean clone (Docker + ../code-clinic)
	docker run --rm --user root -e REPO=$(notdir $(CURDIR)) -v /var/run/docker.sock:/var/run/docker.sock \
	  -v act-cache:/cache -v "$(CURDIR):/src:ro" -v "$(CURDIR)/../code-clinic:/hub:ro" \
	  $(IMAGE) bash /hub/tools/act-local.sh

all: lint test manifests security docs
