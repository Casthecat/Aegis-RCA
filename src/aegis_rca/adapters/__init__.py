"""Read-only public telemetry adapters; constructors never contact a backend."""
from .common import CatalogResult
from .http import AdapterError, JsonHttp, SchemaError
from .jaeger import JaegerAdapter
from .opensearch import OpenSearchAdapter, OpenSearchFields
from .prometheus import PrometheusAdapter

__all__ = ["AdapterError", "CatalogResult", "JaegerAdapter", "JsonHttp", "OpenSearchAdapter",
           "OpenSearchFields", "PrometheusAdapter", "SchemaError"]
