#!/usr/bin/env python3
"""Attach existing ECS APIs without taking ownership of teammate ECS resources.

Defaults to a dry run. Run from this directory after tofu apply. The service
owners must carry the attachments/auth setting into their own infrastructure.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess


def aws(*args):
    return json.loads(subprocess.check_output(["aws", *args, "--output", "json"], text=True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", default="/private/tmp/ticketto-gateway-backup")
    options = parser.parse_args()
    configs = json.loads(subprocess.check_output(
        ["tofu", "output", "-json", "service_attachments"], text=True
    ))
    backup = Path(options.backup_dir)
    if options.apply:
        backup.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, cfg in configs.items():
        result = aws("ecs", "describe-services", "--cluster", cfg["cluster"],
                     "--services", cfg["service"])
        if result.get("failures") or len(result["services"]) != 1:
            raise RuntimeError(f"Cannot find {cfg['service']}")
        service = result["services"][0]
        attachment = {"targetGroupArn": cfg["target_group_arn"],
                      "containerName": cfg["container"], "containerPort": cfg["port"]}
        existing = service.get("loadBalancers", [])
        attachments = existing if attachment in existing else [*existing, attachment]
        task_arn = service["taskDefinition"]
        task = None
        if name == "ticket":
            task = aws("ecs", "describe-task-definition", "--task-definition", task_arn)["taskDefinition"]
            container = next(c for c in task["containerDefinitions"] if c["name"] == cfg["container"])
            if any(s["name"] == "AUTH_MODE" for s in container.get("secrets", [])):
                raise RuntimeError("Ticket AUTH_MODE is secret-managed; reconcile before changing it")
            env = container.setdefault("environment", [])
            if any(e["name"] == "AUTH_MODE" and e["value"] == "gateway" for e in env):
                task = None
            else:
                container["environment"] = [e for e in env if e["name"] != "AUTH_MODE"] + [
                    {"name": "AUTH_MODE", "value": "gateway"}]
        if attachments == existing and task is None:
            print(f"{name}: already attached and configured")
            continue
        print(f"{name}: attach port {cfg['port']} to {cfg['target_group_arn']}"
              + ("; set AUTH_MODE=gateway" if task else ""))
        if not options.apply:
            continue
        backup_file = backup / f"{name}-service.json"
        # Preserve the first pre-change snapshot for rollback.
        if not backup_file.exists():
            with backup_file.open("x") as file:
                os.chmod(backup_file, 0o600)
                json.dump(service, file, indent=2)
        if task is not None:
            allowed = {"family", "taskRoleArn", "executionRoleArn", "networkMode",
                       "containerDefinitions", "volumes", "placementConstraints",
                       "requiresCompatibilities", "cpu", "memory", "pidMode", "ipcMode",
                       "proxyConfiguration", "inferenceAccelerators", "ephemeralStorage",
                       "runtimePlatform", "enableFaultInjection"}
            request = {k: v for k, v in task.items() if k in allowed}
            # No task definition values (which may include secret references) are printed.
            result = aws("ecs", "register-task-definition", "--cli-input-json", json.dumps(request))
            task_arn = result["taskDefinition"]["taskDefinitionArn"]
        result = aws("ecs", "update-service", "--cluster", cfg["cluster"],
                     "--service", cfg["service"], "--load-balancers", json.dumps(attachments),
                     "--task-definition", task_arn, "--health-check-grace-period-seconds", "90")
        print(f"{name}: rollout requested ({result['service']['taskDefinition']})")


if __name__ == "__main__":
    main()
