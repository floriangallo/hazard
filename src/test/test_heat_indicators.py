from datetime import datetime
import json
import logging, os, sys
import logging.handlers
from typing import Dict
from dask.distributed import Client, LocalCluster
import pytest
from pytest import approx

import fsspec.implementations.local as local # type: ignore
from hazard.docs_store import DocStore, HazardModels # type: ignore
from hazard.map_builder import MapBuilder
from hazard.models.work_loss import WorkLossIndicator
from hazard.protocols import OpenDataset, WriteDataset
import hazard.utilities.zarr_utilities as zarr_utilities
from hazard.sources.osc_zarr import OscZarr
from hazard.sources.nex_gddp_cmip6 import NexGddpCmip6
from hazard.models.degree_days import BatchItem, DegreeDays
from .utilities import test_output_dir
import numpy as np
import pandas as pd # type: ignore
import s3fs # type: ignore
import xarray as xr
import zarr # type: ignore

from test.utilities import TestSource, TestTarget, _create_test_dataset_averaged, _create_test_datasets 


def test_degree_days_mocked():
    """Test degree days calculation based on mocked data."""
    gcm = "NorESM2-MM"
    scenario = "ssp585"
    year = 2030
    source = TestSource(_create_test_datasets())
    target = TestTarget()
    # cut down the transform
    model = DegreeDays(window_years=2, gcms=[gcm], scenarios=[scenario], central_years=[year])  
    model.run_all(source, target)
    with source.open_dataset_year(gcm, scenario, "tasmax", 2029) as y0:
        with source.open_dataset_year(gcm, scenario, "tasmax", 2030) as y1:
            scale = 365.0 / len(y0.time)
            deg0 = scale * xr.where(y0.tasmax > (32 + 273.15), y0.tasmax - (32 + 273.15), 0).sum(dim=["time"])
            deg1 = scale * xr.where(y1.tasmax > (32 + 273.15), y1.tasmax - (32 + 273.15), 0).sum(dim=["time"])
            expected = (deg0 + deg1) / 2 
    assert expected.values == approx(target.dataset.values)


def test_zarr_read_write(test_output_dir):
    """Test that an xarray can be stored in xarray's native zarr format and then
    read from the zarr array alone using attributes and ignoring coordinates.
    """
    ds = _create_test_dataset_averaged()
    store = zarr.DirectoryStore(os.path.join(test_output_dir, 'hazard_test', 'hazard.zarr'))
    source = OscZarr(store=store)
    source.write("test", ds.tasmax)
    #ds.to_zarr(store, compute=True, group="test", mode="w", consolidated=False)
    res = source.read_floored("test", [0.0, 1.0], [1.0, 2.0])
    np.testing.assert_array_equal(res, [308., 302.])
    

@pytest.mark.skip(reason="inputs large and downloading slow")
def test_degree_days(test_output_dir):
    """Cut-down but still *slow* test that performs downloading of real datasets."""
    gcm = "NorESM2-MM"
    scenario = "ssp585"
    years = [2028, 2029, 2030]
    download_test_datasets(test_output_dir, gcm, scenario, years)
    # source: read downloaded datasets from local file system
    fs = local.LocalFileSystem()
    source = NexGddpCmip6(root=os.path.join(test_output_dir, NexGddpCmip6.bucket), fs=fs)
    # target: write zarr to load fine system
    store = zarr.DirectoryStore(os.path.join(test_output_dir, 'hazard', 'hazard.zarr'))
    target = OscZarr(store=store)
    # cut down the model and run
    model = DegreeDays(window_years=1, gcms=[gcm], scenarios=[scenario], central_years=[years[0]])
    model.run_all(source, target)
    # check one point...
    path = model._item_path(BatchItem(gcm, scenario, years[1]))
    calculated = target.read_floored(path, [32.625], [15.625])
    # against expected:
    with source.open_dataset_year(gcm, scenario, "tasmax", years[0]) as y0:
        with source.open_dataset_year(gcm, scenario, "tasmax", years[1]) as y1:
            assert y0.lat[302].values == approx(15.625)
            assert y0.lon[130].values == approx(32.625)
            scale = 365.0 / len(y0.time)
            y0p, y1p = y0.tasmax[:, 302, 130].values, y1.tasmax[:, 302, 130].values
            deg0 = scale * xr.where(y0p > (32 + 273.15), y0p - (32 + 273.15), 0).sum()
            deg1 = scale * xr.where(y1p > (32 + 273.15), y1p - (32 + 273.15), 0).sum()
            expected = (deg0 + deg1) / 2 
    assert calculated == approx(expected)


@pytest.mark.skip(reason="inputs large and downloading slow")
def test_work_loss(test_output_dir):
    """Cut-down but still *slow* test that performs downloading of real datasets."""
    gcm = "NorESM2-MM"
    scenario = "ssp585"
    years = [2028, 2029, 2030]
    download_test_datasets(test_output_dir, gcm, scenario, years, indicators=["tas", "hurs"])
    # source: read downloaded datasets from local file system
    fs = local.LocalFileSystem()
    source = NexGddpCmip6(root=os.path.join(test_output_dir, NexGddpCmip6.bucket), fs=fs)
    # target: write zarr to load fine system
    store = zarr.DirectoryStore(os.path.join(test_output_dir, 'hazard', 'hazard.zarr'))
    target = OscZarr(store=store)
    # cut down the model and run
    model = WorkLossIndicator(window_years=3, gcms=[gcm], scenarios=[scenario], central_years=[years[1]])
    resources = list(model.inventory())
    models = HazardModels(hazard_models=resources)
    json_str = json.dumps(models.dict(), indent=4) # pretty print

    local_fs = local.LocalFileSystem()
    docs_store = DocStore(bucket=test_output_dir, fs=local_fs, prefix="hazard_test")

    docs_store.update_inventory(model.inventory())
    model.run_all(source, target)


@pytest.mark.skip(reason="just example")
def test_example_run_degree_days():
    zarr_utilities.set_credential_env_variables() 

    docs_store = DocStore(prefix="hazard_test")
    json = docs_store.read_inventory_json()

    cluster = LocalCluster(processes=False)
    client = Client(cluster)

    gcm = "NorESM2-MM"
    scenario = "ssp585"
    year = 2030
    source = NexGddpCmip6()
    target = OscZarr(prefix="hazard_test") # test prefix is "hazard_test"; main one "hazard"
    # cut down the transform
    model = DegreeDays(window_years=1, gcms=[gcm], scenarios=[scenario], central_years=[year])

    docs_store.update_inventory(model.inventory())

    items = list(model.batch_items())
    model.run_single(items[0], source, target, client=client)
    assert True


def download_test_datasets(test_output_dir, gcm, scenario, years, indicators=["tasmax"]):
    store = NexGddpCmip6()
    s3 = s3fs.S3FileSystem(anon=True)
    for year in years:
        for indicator in indicators:
            path, _ = store.path(gcm, scenario, indicator, year)
            if not os.path.exists(os.path.join(test_output_dir, path)):
                s3.download(path, os.path.join(test_output_dir, path))
    assert True


@pytest.mark.skip(reason="just example")
def test_load_dataset(test_output_dir):    
    fs = local.LocalFileSystem()
    store = NexGddpCmip6(root=os.path.join(test_output_dir, "nex-gddp-cmip6"), fs=fs)
    with store.open_dataset_year("NorESM2-MM", "ssp585", "tasmax", 2029) as ds:
        print(ds)
    assert True
