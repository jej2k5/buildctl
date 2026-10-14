"""Supervisor tests."""
from buildctl.supervisor import phases

# The Relay fixture is a macOS operator's config (host_cli for both agents). The kernel tests
# exercise everything else about it on every platform; test_linux_backend covers the refusal.
phases.host_cli_supported = lambda: True
