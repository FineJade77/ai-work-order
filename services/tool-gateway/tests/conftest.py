import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader


@pytest.fixture
def anyio_backend():
    return "asyncio"


# Like the tracer provider, the meter provider can be set once per process. Metric data is
# cumulative, so tests compare readings before and after (metric_total).
_METRIC_READER = InMemoryMetricReader()
metrics.set_meter_provider(MeterProvider(metric_readers=[_METRIC_READER]))


def metric_total(name: str, **labels: object) -> float:
    """Sum of a counter, or count of a histogram, over the points whose labels include `labels`
    (a "." in a label key is written "__", e.g. app__tool; values compared as given)."""
    wanted = {k.replace("__", "."): v for k, v in labels.items()}
    data = _METRIC_READER.get_metrics_data()
    total = 0.0
    for resource in data.resource_metrics if data else []:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name != name:
                    continue
                for point in metric.data.data_points:
                    if all(point.attributes.get(k) == v for k, v in wanted.items()):
                        total += getattr(point, "count", None) or getattr(point, "value", 0)
    return total
