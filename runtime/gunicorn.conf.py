import os

bind = f"[::]:{int(os.environ.get('PORT', '8080'))}"
workers = 2
threads = 4
timeout = 30
graceful_timeout = 20
limit_request_line = 2048
limit_request_field_size = 8192
limit_request_fields = 32
worker_tmp_dir = "/tmp"
# Request URLs can carry cursors; neither bodies nor headers are logged.
accesslog = None
errorlog = "-"
loglevel = "warning"
forwarded_allow_ips = ""
