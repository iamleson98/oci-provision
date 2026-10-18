# oci-provision - operational makefile (optional; the primary flow is GitHub
# Actions - see docs/CI_DEPLOYMENT.md. These targets are for laptop/Cloud
# Shell users who prefer local control.)
#
# Typical local flow:
#   make tf-init tf-apply  # provision VMs (needs OCI_* env - see scripts/tf_init.sh)
#   make deploy            # rsync + install controller on the A1 worker
#   make status            # open the status page (SSH tunnel)
#
# All hosts can be overridden: make deploy HOST=1.2.3.4 KEY=~/.ssh/id_ed25519

SHELL := /bin/bash
KEY   ?= ~/.ssh/id_ed25519
SSH_OPTS := -o StrictHostKeyChecking=accept-new

TF_DIR := terraform
-include $(TF_DIR)/hosts.mk   # written by tf-apply, pins HOST / WAKER_HOST

HOST        ?= $(shell cd $(TF_DIR) && terraform output -raw a1_public_ip 2>/dev/null)
WAKER_HOST  ?= $(shell cd $(TF_DIR) && terraform output -raw waker_public_ip 2>/dev/null)

define SSH
        ssh $(SSH_OPTS) -i $(KEY) ubuntu@$(1)
endef

.PHONY: help
help: ## Show this help
        @grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ------------------------------------------------------------- terraform ---
.PHONY: tf-init
tf-init: ## terraform init (remote state in OCI Object Storage; needs OCI_* env vars - scripts/tf_init.sh)
        @test -n "$$OCI_NAMESPACE" || (echo "export OCI_NAMESPACE/OCI_REGION/OCI_TENANCY_OCID/OCI_USER_OCID/OCI_FINGERPRINT/OCI_PRIVATE_KEY first (docs/CI_DEPLOYMENT.md)"; exit 2)
        bash scripts/tf_init.sh

.PHONY: tf-plan
tf-plan: ## terraform plan
        cd $(TF_DIR) && terraform plan

.PHONY: tf-apply
tf-apply: ## terraform apply, then record host IPs in terraform/hosts.mk
        cd $(TF_DIR) && terraform apply
        @{ echo "HOST := $(shell cd $(TF_DIR) && terraform output -raw a1_public_ip 2>/dev/null)"; \
           echo "WAKER_HOST := $(shell cd $(TF_DIR) && terraform output -raw waker_public_ip 2>/dev/null)"; } > $(TF_DIR)/hosts.mk
        @echo "-> IPs saved to $(TF_DIR)/hosts.mk"
        @echo "-> A1:      $(shell cd $(TF_DIR) && terraform output -raw a1_public_ip 2>/dev/null)"
        @echo "-> waker:   $(shell cd $(TF_DIR) && terraform output -raw waker_public_ip 2>/dev/null)"

.PHONY: tf-destroy
tf-destroy: ## terraform destroy
        cd $(TF_DIR) && terraform destroy

.PHONY: hunt
hunt: ## fight 'Out of capacity' with the retry hunter (needs COMPARTMENT=ocid1...)
        @test -n "$(COMPARTMENT)" || (echo "usage: make hunt COMPARTMENT=ocid1.compartment.oc1..xxx [AD=0]"; exit 2)
        bash scripts/capacity_hunter.sh --compartment $(COMPARTMENT) --ssh-key $(KEY).pub $(if $(SUBNET),--subnet $(SUBNET),)

# --------------------------------------------------------------- deploy ---
.PHONY: wait-ssh
wait-ssh: ## Wait until cloud-init finishes on both VMs
        @echo "waiting for the A1 worker (cloud-init)..."
        @until $(call SSH,$(HOST)) 'cloud-init status --wait >/dev/null 2>&1 || true; echo ok' 2>/dev/null | grep -q ok; do sleep 10; done
        @if [[ -n "$(WAKER_HOST)" ]]; then \
          echo "waiting for the waker..."; \
          until $(call SSH,$(WAKER_HOST)) 'cloud-init status --wait >/dev/null 2>&1 || true; echo ok' 2>/dev/null | grep -q ok; do sleep 10; done; \
        fi
        @echo "both VMs are up."

.PHONY: deploy
deploy: ## rsync repo + install controller on the A1 worker
        @test -n "$(HOST)" || (echo "HOST unknown - run make tf-apply first or pass HOST=x.x.x.x"; exit 2)
        $(call SSH,$(HOST)) 'sudo mkdir -p /opt/oci-runner && sudo chown ubuntu /opt/oci-runner'
        rsync -az --delete -e "ssh $(SSH_OPTS) -i $(KEY)" \
          --exclude .git --exclude .terraform --exclude terraform.tfstate* --exclude hosts.mk \
          ./ ubuntu@$(HOST):/opt/oci-runner/
        $(call SSH,$(HOST)) 'sudo bash /opt/oci-runner/deploy/install.sh controller'

.PHONY: deploy-waker
deploy-waker: ## rsync repo + install waker on the AMD micro
        @test -n "$(WAKER_HOST)" || (echo "WAKER_HOST unknown - run make tf-apply first or pass WAKER_HOST=x.x.x.x"; exit 2)
        $(call SSH,$(WAKER_HOST)) 'sudo mkdir -p /opt/oci-runner-waker && sudo chown ubuntu /opt/oci-runner-waker'
        rsync -az --delete -e "ssh $(SSH_OPTS) -i $(KEY)" \
          --exclude .git --exclude .terraform --exclude terraform.tfstate* --exclude hosts.mk \
          ./ ubuntu@$(WAKER_HOST):/opt/oci-runner-waker/
        $(call SSH,$(WAKER_HOST)) 'sudo bash /opt/oci-runner-waker/deploy/install.sh waker'

.PHONY: setup-secrets
setup-secrets: ## Write GitHub credentials on both VMs (interactive)
        bash scripts/setup_secrets.sh --host $(HOST) --waker-host $(WAKER_HOST)

# ----------------------------------------------------------------- ops ---
.PHONY: ssh
ssh: ## SSH into the A1 worker
        $(call SSH,$(HOST))

.PHONY: ssh-waker
ssh-waker: ## SSH into the waker
        $(call SSH,$(WAKER_HOST))

.PHONY: logs
logs: ## Tail controller logs
        $(call SSH,$(HOST)) 'docker logs -f --tail 100 oci-runner-controller'

.PHONY: status
status: ## Open the status page through an SSH tunnel (Ctrl-C to close)
        @echo "tunnel ready -> http://localhost:8080  (Ctrl-C to close)"
        ssh -N -L 8080:localhost:8080 -i $(KEY) ubuntu@$(HOST)

.PHONY: status-waker
status-waker: ## Open the WAKER status page through an SSH tunnel
        @echo "tunnel ready -> http://localhost:8080  (Ctrl-C to close)"
        ssh -N -L 8080:localhost:8080 -i $(KEY) ubuntu@$(WAKER_HOST)

.PHONY: restart
restart: ## Restart the controller
        $(call SSH,$(HOST)) 'sudo systemctl restart oci-runner.service'

.PHONY: rebuild
rebuild: ## Rebuild images and restart the controller
        $(call SSH,$(HOST)) 'cd /opt/oci-runner && docker build -q -t oci-runner/runner:latest runner-image/ && docker compose -f deploy/compose.yml build controller && sudo systemctl restart oci-runner.service'

.PHONY: doctor
doctor: ## Run the health check on the A1 worker
        $(call SSH,$(HOST)) 'bash /opt/oci-runner/scripts/doctor.sh || true'

.PHONY: prune
prune: ## Force a docker prune now
        $(call SSH,$(HOST)) 'docker system prune -af --filter "label!=oci-runner.keep" && docker volume prune -f && docker builder prune -af'

.PHONY: app-install-id
app-install-id: ## Find the GitHub App installation id: make app-install-id TARGET=owner/repo
        @test -n "$(TARGET)" || (echo "usage: make app-install-id TARGET=owner/repo"; exit 2)
        $(call SSH,$(HOST)) 'docker exec oci-runner-controller python app_install_id.py $(TARGET)'

.PHONY: clean
clean: ## Remove local build artifacts
        rm -rf $(TF_DIR)/hosts.mk $(TF_DIR)/.terraform $(TF_DIR)/terraform.tfstate*
