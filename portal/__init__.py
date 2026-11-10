import warnings

# eventlet announces its own deprecation on import, in every process that
# loads it (the service, and the CLIs through the app). The portal uses it
# knowingly; the notice only buries real output in installs and logs.
warnings.filterwarnings("ignore", message=r"\s*Eventlet is deprecated")
