"""Perform calculations of the Amundsen Sea Low Index"""

import argparse
import datetime
import logging
import os
from pathlib import Path
from typing import Mapping

import joblib
import pandas as pd
import skimage
from tqdm import tqdm
import xarray as xr

from .params import ASL_REGION, CALCULATION_VERSION, SOFTWARE_VERSION, MASK_THRESHOLD
from .plot import plot_lows
from .utils import tqdm_joblib

logger = logging.getLogger(__name__)

__all__ = ["ASLICalculator"]

def asl_sector_mean(
    da: xr.DataArray, mask: xr.DataArray, asl_region: Mapping[str, float] = ASL_REGION
) -> xr.DataArray:
    """
    Mean of data array `da`, masked by land-sea mask `mask` within bounded region `asl_region`.
    `asl_region` defaults to Amundsen Sea bounds defined in this package as `ASL_REGION`.
    """

    return (
        da.where(mask < MASK_THRESHOLD)
        .sel(
            latitude=slice(asl_region["north"], asl_region["south"]),
            longitude=slice(asl_region["west"], asl_region["east"]),
        )
        .mean()
        .values
    )


def get_lows(da: xr.DataArray, mask: xr.DataArray) -> pd.DataFrame:
    """
    Finds local minima in data array da, ignoring land from land-sea mask, mask.

    Args:
        da (xr.DataArray): data array containing mean sea level pressure fields.
        mask (xr.DataArray): data array containing land-sea mask

    Returns:
        pd.DataFrame: containing columns 'time','lon','lat','ActCenPres','SectorPres','RelCenPres'
    """

    lons, lats = da.longitude.values, da.latitude.values

    sector_mean_pres = asl_sector_mean(da, mask)
    threshold = sector_mean_pres

    date = datetime.datetime.strptime(str(da.date.values), "%Y%m%d")

    time_str = date.strftime("%Y-%m-%d")

    # fill land in with highest value to limit lows being found here
    da_max = da.max().values
    da = da.where(mask < MASK_THRESHOLD).fillna(da_max)

    invert_data = (da * -1.0).values  # search for peaks rather than minima

    if threshold is None:
        threshold_abs = invert_data.mean()
    else:
        threshold_abs = (
            threshold * -1
        )  # define threshold cut-off for peaks (inverted lows)

    minima_yx = skimage.feature.peak_local_max(
        invert_data,  # input data
        min_distance=5,  # peaks are separated by at least min_distance
        num_peaks=3,  # maximum number of peaks
        exclude_border=False,  # excludes peaks from within min_distance pixels of the border
        threshold_abs=threshold_abs,  # minimum intensity of peaks
    )

    minima_lat, minima_lon, pressure = [], [], []
    for minima in minima_yx:
        minima_lat.append(lats[minima[0]])
        minima_lon.append(lons[minima[1]])
        pressure.append(da.values[minima[0], minima[1]])

    df = pd.DataFrame()
    df["lat"] = minima_lat
    df["lon"] = minima_lon
    df["ActCenPres"] = pressure
    df["SectorPres"] = sector_mean_pres
    df["time"] = time_str
    df["DataSource"] = "ERA5T" if da.expver.values == "0005" else "ERA5"

    ### Add relative central pressure (Hosking et al. 2013)
    df["RelCenPres"] = df["ActCenPres"] - df["SectorPres"]

    ### re-order columns
    df = df[["time", "lon", "lat", "ActCenPres", "SectorPres", "RelCenPres", "DataSource"]]

    ### clean-up DataFrame
    df = df.reset_index(drop=True)

    return df


def _get_lows_by_time(da: xr.DataArray, slice_by: str, t: int, mask: xr.DataArray):
    if slice_by == "season":
        da_t = da.isel(season=t)
    elif slice_by == "time":
        da_t = da.isel(time=t)

    return get_lows(da_t, mask)


def define_minima_per_time_in_region(
    df: pd.DataFrame, region: Mapping[str, float] = ASL_REGION
) -> pd.DataFrame:
    """
    From a dataframe of multiple minima per time period, selects the lowest minimum within each time period,
    contained within bounding box: region (defaults to ASL_REGION)
    """
    ### select only those points within ASL box
    df2 = df[
        (df["lon"] > region["west"])
        & (df["lon"] < region["east"])
        & (df["lat"] > region["south"])
        & (df["lat"] < region["north"])
    ]

    ### For each time, get the row with the lowest minima_number
    df2 = df2.loc[df2.groupby("time")["ActCenPres"].idxmin()]

    df2 = df2.reset_index(drop=True)

    return df2


def slice_region(
    da: xr.DataArray, region: Mapping[str, float] = ASL_REGION, border: int = 8
):
    """
    Select region from within data array, with surrounding border.
    """
    da = da.sel(
        latitude=slice(region["north"] + border, region["south"] - border),
        longitude=slice(region["west"] - border, region["east"] + border),
    )
    return da


def season_mean(ds, calendar="standard"):
    # # Make a DataArray with the number of days in each month, size = len(time)
    # month_length = ds.time.dt.days_in_month

    # # Calculate the weights by grouping by 'time.season'
    # weights = (
    #     month_length.groupby("time.season") / month_length.groupby("time.season").sum()
    # )

    # # Test that the sum of the weights for each season is 1.0
    # np.testing.assert_allclose(weights.groupby("time.season").sum().values, np.ones(4))

    # # Calculate the weighted average
    # return (ds * weights).groupby("time.season").sum(dim="time")

    return ds.resample(time="QS-Mar").mean("time")


class ASLICalculator:
    """
    Object to handle calculations of the Amundsen Sea Low Index
    """

    def __init__(
        self,
        data_dir: str = "./data",
        mask_filename: str = "era5_lsm.nc",
        msl_pattern: str = "monthly/era5_mean_sea_level_pressure_monthly_*.nc",
    ) -> None:
        self.data_dir = Path(data_dir)
        self.mask_filename = mask_filename
        self.msl_pattern = msl_pattern

        self.land_sea_mask = None
        self.raw_msl_data = None
        self.masked_msl_data = None
        self.sliced_msl = None
        self.sliced_masked_msl = None
        self.asl_df = None

    def read_mask_data(self):
        """
        Reads in the Land-Sea mask file from <data_dir>/<mask_filename>
        """

        self.land_sea_mask = xr.open_dataset(
            Path(self.data_dir, self.mask_filename)
        ).lsm.squeeze()

    def read_msl_data(self, include_era5t: bool=False):
        """
        Reads in the MSL (mean sea level pressure) files from <data_dir>/<msl_pattern>.
        msl_pattern should be a file path under <data_dir> or a pattern (also within <data_dir>) as taken by xarray.open_mfdataset()
        eg monthly/era5_mean_sea_level_pressure_monthly_*.nc

        Args:
            include_era5t(bool): Controls whether ERA5T initial release data is included. (Default: False)
        """

        if self.land_sea_mask is None:
            logger.error("Must read in land-sea mask before mean sea level data.")
            return

        raw_msl_data_path = os.path.join(self.data_dir, self.msl_pattern)
        self.raw_msl_data = xr.open_mfdataset(raw_msl_data_path).msl

        # expver coordinate indicates whether data is initial or final release
        # expver=0001 - final, expver=0005 initial
        if hasattr(self.raw_msl_data, "expver") and not include_era5t:
            months = []
            for month in self.raw_msl_data:
                if month.expver.values == "0001":
                    months.append(month)
            self.raw_msl_data = xr.concat(months, dim="valid_time")
        

        self.masked_msl_data = self.raw_msl_data.where(
            self.land_sea_mask < MASK_THRESHOLD
        )

        ### slice area around ASL region
        sliced_msl = slice_region(self.raw_msl_data)
        self.sliced_masked_msl = slice_region(self.masked_msl_data)

        # change units
        sliced_msl = sliced_msl / 100.0
        self.sliced_msl = sliced_msl.assign_attrs(units="hPa")

    def read_data(self, include_era5t:bool = False):
        """
        Convenience method for reading in both mask and msl data files.

        Args:
            include_era5t(bool): Controls whether ERA5T intial release data is included. (Default: False)
        """
        self.read_mask_data()
        self.read_msl_data(include_era5t)

    def calculate(self, n_jobs: int = 1) -> pd.DataFrame:
        """
        From loaded mean sea level pressure data and land-sea mask, runs the calculation of minima.

        Args:
            n_jobs (int, optional): Number of processes to use for parallel calculation. Defaults to 1.

        Returns:
            pd.DataFrame: dataframe containing locations of pressure minima, mean pressure.
        """

        if self.sliced_msl is None:
            raise Exception(f"self.sliced_msl is {self.sliced_msl}, have you run .read_data()?")

        if "season" in self.sliced_msl.dims:
            ntime = 4
            slice_by = "season"
        if "valid_time" in self.sliced_msl.dims:
            self.sliced_msl = self.sliced_msl.rename({'valid_time': 'time'})
        if "time" in self.sliced_msl.dims:
            ntime = self.sliced_msl.time.shape[0]
            slice_by = "time"

        with tqdm_joblib(tqdm(total=ntime)) as progress_bar:
            lows_per_time = joblib.Parallel(n_jobs=n_jobs)(
                joblib.delayed(_get_lows_by_time)(
                    self.sliced_msl, slice_by, t, self.land_sea_mask
                )
                for t in range(ntime)
            )

        self.all_lows_dfs = pd.concat(lows_per_time, ignore_index=True)

        self.asl_df = define_minima_per_time_in_region(self.all_lows_dfs)
        return self.asl_df

    def to_csv(self, filename: str) -> None:
        """Writes out ASLICalculator.asl_df as a CSV file with header.

        Args:
            filename (str): filename to write out to, relative to "data_dir".
        """

        filepath = Path(self.data_dir, filename)

        # TODO handle source data, time_averaging and writing out all lows
        # if (len(self.all_lows_dfs.time.unique()) < 200):
        #     if '-TESTING' not in version_id:
        #         version_id = version_id+'-TESTING'

        # if header == 'asli':
        #     fname = indata+'/asli_'+time_averaging+'_v'+version_id+'.csv'
        # if header == 'all_lows':
        #     fname = indata+'/all_lows_'+time_averaging+'_v'+version_id+'.csv'

        # Set up jinja
        from jinja2 import Environment, PackageLoader, select_autoescape

        env = Environment(loader=PackageLoader("asli"), autoescape=select_autoescape())
        template = env.get_template("asli_data.csv.template")

        header = template.render(
            calculation_version=CALCULATION_VERSION,
            software_version=SOFTWARE_VERSION,
            data_version=datetime.datetime.now().strftime("%Y%m%d"),
        )

        logger.info(f"Writing csv to {filepath}")
        with open(filepath, "w") as f:
            f.writelines(header)
            self.asl_df.to_csv(f, index=False)


    def import_from_csv(self, filename: (str|Path),force: bool = False):
        """
        Import a csv file exported from the .export_df method, for example to plot data from a previous session.

        Args:
            filename (str|Path, required): Path to csv file containing ASL dataframe.
            force (bool, optional): Overwrite existing calculations in this object. Defaults to False.
        """

        if self.asl_df is not None and not force:
            logger.warn("Calculation dataframe has existing values, set force=True to overwrite with import.")
            return
        
        filepath = Path(self.data_dir, filename)
        
        logger.info(f"Importing ASL values from {filepath}")
        self.asl_df = pd.read_csv(filepath, header=27)


    def plot_region_all(self):
        """Plots mean sea level pressure fields for the Amundsen Sea with identified low pressure and bounding box."""

        if self.asl_df is None:
            raise Warning(f"ASL calculation dataframe is {self.as_df}, can not plot. \
                          Try running .calculate() first.")
        plot_lows(self.masked_msl_data, self.asl_df, regionbox=ASL_REGION)

    def plot_region_year(self, year: int):
        """As for plot_region_all but selects only year

        Args:
            year (int): year to plot
        """
        if self.asl_df is None:
            raise Warning(f"ASL calculation dataframe is {self.as_df}, can not plot. \
                          Try running .calculate() first.")
        
        da = self.masked_msl_data.sel(
            time=slice(str(year) + "-01-01", str(year) + "-12-01")
        )
        df = self.asl_df.sel(time=slice(str(year) + "-01-01", str(year) + "-12-01"))
        plot_lows(da, df, year=year, regionbox=ASL_REGION)


def _cli_common_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Parse command-line args common to calculation and plotting."""
    parser.add_argument(
        "-d",
        "--datadir",
        nargs="?",
        type=str,
        default="./data",
        help="Path to directory in which to put downloaded data. (Default: ./data)",
    )
    parser.add_argument(
        "-m",
        "--mask",
        nargs="?",
        type=str,
        default="era5_lsm.nc",
        help="Land-sea mask file path relative to <datadir>. (Default: era5_lsm.nc)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        help="Output file path for CSV, relative to <datadir>.",
    )
    parser.add_argument(
        "msl_files",
        nargs="*",
        type=str,
        help="Path or glob pattern relative to <datadir> for file(s) containing mean sea level pressure.",
    )  # msl files/pattern

    return parser


def _get_cli_plot_args():
    """Parse command-line arguments for cli_plot()"""
    
    parser = argparse.ArgumentParser(
        prog="asli_plot",
        description="Plot Amundsen sea low with mean sea level pressure fields.",
    )
    parser.add_argument(
        "-i",
        "--input",
        nargs="?",
        type=str,
        help="Input CSV file, relative to <datadir>.",
    )
    parser = _cli_common_args(parser)

    return parser.parse_args()


def _get_cli_calc_args():
    """Parse command-line arguments for cli_calc()"""

    parser = argparse.ArgumentParser(
        prog="asli_calc",
        description="Calculates the Amundsen Sea Low from mean sea level pressure fields.",
    )
    parser = _cli_common_args(parser)
    parser.add_argument(
        "-e",
        "--era5t",
        action="store_true",
        help="When present, this flag enables the inclusion of ERA5T initial release data as well as finalised ERA5 data."
    )
    parser.add_argument(
        "-n",
        "--numjobs",
        nargs="?",
        type=int,
        default=1,
        help="Number of processes used by joblib in parallel calculation.",
    )
    
    return parser.parse_args()

def _cli_plot():
    """Command-line interface to ASLI plotting."""

    args = _get_cli_plot_args()

    a = ASLICalculator(args.datadir, args.mask, args.msl_files[0])
    a.read_mask_data()
    a.read_msl_data()
    a.import_from_csv(args.input)
    a.plot_region_all()

def _cli_calc():
    """Command-line interface to ASL calculation."""

    args = _get_cli_calc_args()

    a = ASLICalculator(args.datadir, args.mask, args.msl_files[0])
    a.read_mask_data()
    a.read_msl_data(include_era5t=args.era5t)
    a.calculate(args.numjobs)

    if args.output:
        a.to_csv(args.output)


if __name__ == "__main__":
    _cli_calc()
