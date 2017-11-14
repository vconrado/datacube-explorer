from __future__ import absolute_import

from datetime import datetime

import flask
import shapely.geometry
import shapely.ops
from cachetools.func import ttl_cache
from datacube.index import index_connect
from datacube.model import Range
from datacube.scripts.dataset import build_dataset_info
from datacube.utils import jsonify_document
from datacube.utils.geometry import CRS
from dateutil import tz
from flask import jsonify, logging
from flask import request
from werkzeug.datastructures import MultiDict

from cubedash import _utils as utils

_HARD_SEARCH_LIMIT = 500

app = flask.Flask('cubedash')
app.register_blueprint(utils.bp)

# Only do expensive queries "once a day"
# Enough time to last the remainder of the work day, but not enough to still be there the next morning
CACHE_LONG_TIMEOUT_SECS = 60 * 60 * 18


def as_json(o):
    return jsonify(jsonify_document(o))


# Thread and multiprocess safe.
# As long as we don't run queries (ie. open db connections) before forking (hence validate=False).
index = index_connect(application_name='cubedash', validate_connection=False)

_LOG = logging.getLogger(__name__)


def next_date(date):
    if date.month == 12:
        return datetime(date.year + 1, 1, 1)

    return datetime(date.year, date.month + 1, 1)


def dataset_to_feature(ds):
    return {
        'type': 'Feature',
        'geometry': ds.extent.to_crs(CRS('EPSG:4326')).__geo_interface__,
        'properties': {
            'id': ds.id,
            'product': ds.type.name,
            'time': ds.center_time
        }
    }


@app.route('/api/datasets/<product>/<int:year>-<int:month>')
@ttl_cache(ttl=CACHE_LONG_TIMEOUT_SECS)
def datasets_as_features(product, year, month):
    start = datetime(year, month, 1)
    time = Range(start, next_date(start))
    datasets = index.datasets.search(product=product, time=time)
    return as_json({
        'type': 'FeatureCollection',
        'features': [dataset_to_feature(ds)
                     for ds in datasets if ds.extent]
    })


@app.route('/api/datasets/<product>/<int:year>-<int:month>/poly')
@ttl_cache(ttl=CACHE_LONG_TIMEOUT_SECS)
def dataset_shape(product, year, month):
    start = datetime(year, month, 1)
    time = Range(start, next_date(start))
    datasets = index.datasets.search(product=product, time=time)

    dataset_shapes = [shapely.geometry.asShape(ds.extent.to_crs(CRS('EPSG:4326')))
                      for ds in datasets if ds.extent]
    return as_json(dict(
        type='Feature',
        geometry=shapely.ops.unary_union(dataset_shapes).__geo_interface__,
        properties=dict(
            dataset_count=len(dataset_shapes)
        )
    ))


@ttl_cache(ttl=CACHE_LONG_TIMEOUT_SECS)
def _timeline_years(from_year, product):
    timeline = index.datasets.count_product_through_time(
        '1 month',
        product=product,
        time=Range(
            datetime(from_year, 1, 1, tzinfo=tz.tzutc()),
            datetime.utcnow()
        )
    )
    return list(timeline)


@ttl_cache(ttl=CACHE_LONG_TIMEOUT_SECS)
def _timelines_platform(platform):
    products = index.datasets.count_by_product_through_time(
        '1 month',
        platform=platform,
        time=Range(
            datetime(1986, 1, 1, tzinfo=tz.tzutc()),
            datetime.utcnow()
        )
    )
    return list(products)


@app.route('/')
def default_redirect():
    """Redirect to default starting page."""
    return flask.redirect(flask.url_for('product_spatial_page', product='ls7_level1_scene'))


@app.route('/<product>/spatial')
def product_spatial_page(product):
    types = index.datasets.types.get_all()
    return flask.render_template(
        'spatial.html',
        products=[p.definition for p in types],
        selected_product=product
    )


@app.route('/<product>/timeline')
def product_timeline_page(product):
    return flask.render_template(
        'timeline.html',
        timeline=_timeline_years(1986, product),
        products=[p.definition for p in index.datasets.types.get_all()],
        selected_product=product
    )


@app.route('/<product>/datasets')
def product_datasets_page(product: str):
    product_entity = index.products.get_by_name_unsafe(product)
    args = MultiDict(flask.request.args)

    query = utils.query_to_search(args, product=product_entity)
    _LOG.info('Query %r', query)

    # TODO: Add sort option to index API
    datasets = sorted(index.datasets.search(**query, limit=_HARD_SEARCH_LIMIT), key=lambda d: d.center_time)

    if request_wants_json():
        return as_json(dict(
            datasets=[build_dataset_info(index, d) for d in datasets],
        ))
    return flask.render_template(
        'datasets.html',
        products=[p.definition for p in index.datasets.types.get_all()],
        selected_product=product,
        selected_product_e=product_entity,
        datasets=datasets,
        query_params=query
    )


def request_wants_json():
    best = request.accept_mimetypes.best_match(['application/json', 'text/html'])
    return best == 'application/json' and \
           request.accept_mimetypes[best] > \
           request.accept_mimetypes['text/html']


@app.route('/platform/<platform>')
def platform_page(platform):
    return flask.render_template(
        'platform.html',
        product_counts=_timelines_platform(platform),
        products=[p.definition for p in index.datasets.types.get_all()],
        platform=platform
    )


@app.route('/datasets/<uuid:id_>')
def dataset_page(id_):
    dataset = index.datasets.get(id_, include_sources=True)

    source_datasets = {type_: index.datasets.get(dataset_d['id'])
                       for type_, dataset_d in dataset.metadata.sources.items()}

    ordered_metadata = utils.get_ordered_metadata(dataset.metadata_doc)

    return flask.render_template(
        'dataset.html',
        dataset=dataset,
        dataset_metadata=ordered_metadata,
        derived_datasets=index.datasets.get_derived(id_),
        source_datasets=source_datasets
    )