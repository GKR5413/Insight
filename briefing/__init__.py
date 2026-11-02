import os

# Milvus Lite runs a gRPC server in-process; its C core logs harmless "too_many_pings" keepalive
# errors to stderr. Hide them by default (set GRPC_VERBOSITY=ERROR yourself to see gRPC problems).
os.environ.setdefault("GRPC_VERBOSITY", "NONE")
