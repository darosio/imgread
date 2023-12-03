"""Microscopy Data Reader for nima_io Library.

This module provides a set of functions to read microscopy data files,
leveraging the bioformats library and custom processing for metadata and pixel
data.

For detailed function documentation and usage, refer to the Sphinx-generated
documentation.

"""
import collections
import io
import os
import subprocess
import sys
import tempfile
import warnings
from contextlib import contextmanager
from typing import Any, Optional, Protocol

import bioformats  # type: ignore[import-untyped]
import javabridge  # type: ignore[import-untyped]
import jpype  # type: ignore[import-untyped]
import numpy as np
import pims  # type: ignore[import-untyped]
from bioformats import JARS
from lxml import etree  # type: ignore[import-untyped]

# javabridge.start_vm(class_path=bioformats.JARS, run_headless=True)
# javabridge.kill_vm()

# /home/dan/4bioformats/python-microscopy/PYME/IO/DataSources/BioformatsDataSource.py
NUM_VM = 0


def ensure_vm() -> None:
    """Start javabridge VM."""
    global NUM_VM
    if NUM_VM < 1:
        javabridge.start_vm(class_path=JARS, run_headless=True)
        NUM_VM += 1


def release_vm() -> None:
    """Kill javabridge VM."""
    global NUM_VM
    NUM_VM -= 1
    if NUM_VM < 1:
        javabridge.kill_vm()


@contextmanager
def stdout_redirector(stream):
    """Context manager to capure fd-level stdout.

    Taken from:
    https://eli.thegreenplace.net/2015/redirecting-all-kinds-of-stdout-in-python/

    Alternatively see also the following, but did not work
    https://stackoverflow.com/questions/24277488/in-python-how-to-capture-the-stdout-from-a-c-shared-library-to-a-variable

    """
    # The original fd stdout points to. Usually 1 on POSIX systems.
    original_stdout_fd = sys.stdout.fileno()

    def _redirect_stdout(to_fd):
        """Redirect stdout to the given file descriptor."""
        # Flush and close sys.stdout - also closes the file descriptor (fd)
        sys.stdout.close()
        # Make original_stdout_fd point to the same file as to_fd
        os.dup2(to_fd, original_stdout_fd)
        # Create a new sys.stdout that points to the redirected fd
        sys.stdout = io.TextIOWrapper(os.fdopen(original_stdout_fd, "wb"))

    # Save a copy of the original stdout fd in saved_stdout_fd
    saved_stdout_fd = os.dup(original_stdout_fd)
    try:
        # Create a temporary file and redirect stdout to it
        tfile = tempfile.TemporaryFile(mode="w+b")
        _redirect_stdout(tfile.fileno())
        # Yield to caller, then redirect stdout back to the saved fd
        yield
        _redirect_stdout(saved_stdout_fd)
        # Copy contents of temporary file to the given stream
        tfile.flush()
        tfile.seek(0, io.SEEK_SET)
        # Fixed for python3 read() returns byte and not string.
        stream.write(str(tfile.read(), "utf8"))
    finally:
        tfile.close()
        os.close(saved_stdout_fd)


def init_metadata(series_count, file_format):
    """Return an initialized metadata dict.

    Any data file has one file format and contains one or more series.
    Each series can have different metadata (channels, Z, SizeX, etc.).

    Parameters
    ----------
    series_count : int
        Number of series (stacks, images, ...).
    file_format : str
        File format as a string.

    Returns
    -------
    md : dict
        The key "series" is a list of dictionaries; one for each series
        (to be filled).

    """
    md = {"SizeS": series_count, "Format": file_format, "series": []}
    return md


def fill_metadata(md, sr, root):
    """Works when using (java) root metadata.

    For each series return a dict with metadata like SizeX, SizeT, etc.

    Parameters
    ----------
    md : dict
        Initialized dict for metadata.
    sr : int
        Number of series (stacks, images, ...).
    root : ome.xml.meta.OMEXMLMetadataRoot
        OME metadata root.

    """
    for i in range(sr):
        image = root.getImage(i)
        pixels = image.getPixels()
        try:
            psx = round(float(pixels.getPhysicalSizeX().value()), 6)
        except Exception:
            psx = None
        try:
            psy = round(float(pixels.getPhysicalSizeY().value()), 6)
        except Exception:
            psy = None
        try:
            psz = round(float(pixels.getPhysicalSizeZ().value()), 6)
        except Exception:
            psz = None
        try:
            date = image.getAcquisitionDate().getValue()
        except Exception:
            date = None
        try:
            pos = {
                (
                    pixels.getPlane(i).getPositionX().value().doubleValue(),
                    pixels.getPlane(i).getPositionY().value().doubleValue(),
                    pixels.getPlane(i).getPositionZ().value().doubleValue(),
                )
                for i in range(pixels.sizeOfPlaneList())
            }
        except Exception:
            pos = None
        md["series"].append(
            {
                "PhysicalSizeX": psx,
                "PhysicalSizeY": psy,
                "PhysicalSizeZ": psz,
                "SizeX": int(pixels.getSizeX().getValue()),
                "SizeY": int(pixels.getSizeY().getValue()),
                "SizeC": int(pixels.getSizeC().getValue()),
                "SizeZ": int(pixels.getSizeZ().getValue()),
                "SizeT": int(pixels.getSizeT().getValue()),
                "Bits": int(pixels.getSignificantBits().getValue()),
                "Name": image.getName(),
                "Date": date,
                "PositionXYZ": pos,
            }
        )


def tidy_metadata(md) -> None:
    """Move metadata common to all series into principal keys of the metadata dict.

    Parameters
    ----------
    md : dict
        Dict for metadata with all series filled.
        The key "series" is a list of dictionaries containing only metadata
        that are not common among all series. Common metadata are accessible
        as first level keys.

    """
    if len(md["series"]) == 1:
        d = md["series"][0]
        while d:
            k, v = d.popitem()
            md[k] = v
        md.pop("series")
    elif len(md["series"]) > 1:
        keys_samevalue = []
        for k in md["series"][0]:
            ll = [d[k] for d in md["series"]]
            if ll.count(ll[0]) == len(ll):
                keys_samevalue.append(k)
        for k in keys_samevalue:
            for d in md["series"]:
                val = d.pop(k)
            md[k] = val


def read_inf(filepath):
    """Use external showinf.

    Parameters
    ----------
    filepath : path
        File to be parsed.

    Returns
    -------
    md : dict
        Tidied metadata.

    Notes
    -----
    10-40 times slower than all others

    References
    ----------
    http://bioimage-analysis.stanford.edu/guides/3-Loading_microscopy_images/

    """
    # first run to get number of images (i.e. series)
    inf0 = ["showinf", "-nopix", filepath]
    p0 = subprocess.Popen(inf0, stdout=subprocess.PIPE)
    a0 = subprocess.check_output(
        ("grep", "-E", "Series count|file format"), stdin=p0.stdout
    )
    for line in a0.decode("utf8").splitlines():
        if "file format" in line:
            ff = line.rstrip("]").split("[")[1]
        if "Series count" in line:
            sr = int(line.split("=")[1])
    md = init_metadata(sr, ff)
    # second run for xml metadata
    inf = ["showinf", "-nopix", "-omexml-only", filepath]
    p = subprocess.Popen(inf, stdout=subprocess.PIPE)
    stdout = p.communicate()[0]
    parser = etree.XMLParser(recover=True)
    # Parsing trusted microscopy files
    tree = etree.fromstring(stdout, parser)  # noqa: S320
    for child in tree:
        if child.tag.endswith("Image"):
            for grandchild in child:
                if grandchild.tag.endswith("Pixels"):
                    att = grandchild.attrib
                    try:
                        psx = round(float(att["PhysicalSizeX"]), 6)
                    except Exception:
                        psx = None
                    try:
                        psy = round(float(att["PhysicalSizeY"]), 6)
                    except Exception:
                        psy = None
                    try:
                        psz = round(float(att["PhysicalSizeZ"]), 6)
                    except Exception:
                        psz = None
                    try:
                        psx_u = att["PhysicalSizeXUnit"]
                    except Exception:
                        psx_u = None
                    try:
                        psy_u = att["PhysicalSizeYUnit"]
                    except Exception:
                        psy_u = None
                    try:
                        psz_u = att["PhysicalSizeZUnit"]
                    except Exception:
                        psz_u = None
                    md["series"].append(
                        {
                            "PhysicalSizeX": psx,
                            "PhysicalSizeY": psy,
                            "PhysicalSizeZ": psz,
                            "PhysicalSizeXUnit": psx_u,
                            "PhysicalSizeYUnit": psy_u,
                            "PhysicalSizeZUnit": psz_u,
                            "SizeX": int(att["SizeX"]),
                            "SizeY": int(att["SizeY"]),
                            "SizeC": int(att["SizeC"]),
                            "SizeZ": int(att["SizeZ"]),
                            "SizeT": int(att["SizeT"]),
                            "Bits": int(att["SignificantBits"]),
                        }
                    )
        elif child.tag.endswith("Instrument"):
            for grandchild in child:
                if grandchild.tag.endswith("Objective"):
                    att = grandchild.attrib
                    obj_model = att["Model"]
    tidy_metadata(md)
    if "obj_model" in locals():
        md["Obj"] = obj_model
    return md, None


def read_bf(filepath):
    """Use standard bioformats instruction; fails with FEITiff.

    Parameters
    ----------
    filepath : path
        File to be parsed.

    Returns
    -------
    md : dict
        Tidied metadata.

    Notes
    -----
    In this approach the reader reports the last Pixels read (e.g. z=37),
    dimensionorder ...

    """
    omexmlstr = bioformats.get_omexml_metadata(filepath)
    o = bioformats.omexml.OMEXML(omexmlstr)
    sr = o.get_image_count()
    md = init_metadata(sr, "ff")
    for i in range(sr):
        md["series"].append(
            {
                "PhysicalSizeX": round(o.image(i).Pixels.PhysicalSizeX, 6)
                if o.image(i).Pixels.PhysicalSizeX
                else None,
                "PhysicalSizeY": round(o.image(i).Pixels.PhysicalSizeY, 6)
                if o.image(i).Pixels.PhysicalSizeY
                else None,
                "SizeX": o.image(i).Pixels.SizeX,
                "SizeY": o.image(i).Pixels.SizeY,
                "SizeC": o.image(i).Pixels.SizeC,
                "SizeZ": o.image(i).Pixels.SizeZ,
                "SizeT": o.image(i).Pixels.SizeT,
            }
        )
    tidy_metadata(md)
    return md, None


def read_jb(filepath):
    """Use java directly to access metadata.

    Parameters
    ----------
    filepath : path
        File to be parsed.

    Returns
    -------
    md : dict
        Tidied metadata.

    References
    ----------
    Following suggestions at:
    https://github.com/CellProfiler/python-bioformats/issues/23

    """
    rdr = javabridge.JClassWrapper("loci.formats.in.OMETiffReader")()
    rdr.setOriginalMetadataPopulated(True)
    ome_xml_service = javabridge.JClassWrapper("loci.formats.services.OMEXMLService")
    service_factory = javabridge.JClassWrapper("loci.common.services.ServiceFactory")()
    service = service_factory.getInstance(ome_xml_service.klass)
    metadata = service.createOMEXMLMetadata()
    rdr.setMetadataStore(metadata)
    rdr.setId(filepath)
    sr = rdr.getSeriesCount()
    root = metadata.getRoot()
    md = init_metadata(sr, rdr.getFormat())
    fill_metadata(md, sr, root)
    tidy_metadata(md)
    return md, None


def read(filepath: str) -> tuple[dict[str, Any], bioformats.formatreader.ImageReader]:
    """Read a data file picking the correct Format.

    metadata (e.g. channels,time points, ...).

    It uses java directly to access metadata, but the reader is picked by
    loci.formats.ImageReader.

    Parameters
    ----------
    filepath : str
        File to be parsed.

    Returns
    -------
    Tuple[Dict[str, Any], bioformats.formatreader.ImageReader]
        md : dict
            Tidied metadata.
        wrapper : bioformats.formatreader.ImageReader
            A wrapper to the Loci image reader; to be used for accessing data from
            disk.

    Raises
    ------
    FileNotFoundError
        If the specified file is not found.

    Examples
    --------
    >>> javabridge.start_vm(class_path=bioformats.JARS, run_headless=True)
    >>> md, wr = read('../tests/data/multi-channel-time-series.ome.tif')
    >>> md['SizeC'], md['SizeT'], md['SizeX'], md['Format'], md['Bits']
    (3, 7, 439, 'OME-TIFF', 8)
    >>> a = wr.read(c=2, t=6, series=0, z=0, rescale=False)
    >>> a[20,200]
    -1

    """
    if not os.path.isfile(filepath):
        msg = f"File not found: {filepath}"
        raise FileNotFoundError(msg)
    image_reader = bioformats.formatreader.make_image_reader_class()()
    image_reader.allowOpenToCheckType(True)
    # metadata a la java
    ome_xml_service = javabridge.JClassWrapper("loci.formats.services.OMEXMLService")
    service_factory = javabridge.JClassWrapper("loci.common.services.ServiceFactory")()
    service = service_factory.getInstance(ome_xml_service.klass)
    metadata = service.createOMEXMLMetadata()
    image_reader.setMetadataStore(metadata)
    image_reader.setId(filepath)
    sr = image_reader.getSeriesCount()
    # n_t = image_reader.getSizeT() remember it refers to pixs of first series
    root = metadata.getRoot()
    md = init_metadata(sr, image_reader.getFormat())
    fill_metadata(md, sr, root)
    tidy_metadata(md)
    # Make a fake ImageReader and install the one above inside it
    wrapper = bioformats.formatreader.ImageReader(path=filepath, perform_init=False)
    wrapper.rdr = image_reader
    return md, wrapper


def read_pims(filepath):
    """ref: https://docs.openmicroscopy.org/bio-formats/5.9.0/about/index.html.

    Core metadata only includes things necessary to understand the basic
    structure of the pixels:
    - image resolution
    - number of focal planes
    - time points -- (SizeT)
    - channels -- (SizeC)
    and other dimensional axes;
    - byte order
    - dimension order
    - color arrangement (RGB, indexed color or separate channels)
    - and thumbnail resolution.

    NB name and date are not core metadata.

    (series)
    (series, plane) where plane combines z, t and c?

    """
    fs = pims.Bioformats(filepath)
    md = init_metadata(fs.size_series, fs.reader_class_name)
    for s in range(md["SizeS"]):
        fs = pims.Bioformats(filepath, series=s)
        try:
            pos = {
                (
                    fs.metadata.PlanePositionX(s, p),
                    fs.metadata.PlanePositionY(s, p),
                    fs.metadata.PlanePositionZ(s, p),
                )
                for p in range(fs.metadata.PlaneCount(s))
            }
        except AttributeError:
            pos = None
        md["series"].append(
            {
                "SizeX": fs.sizes["x"],
                "SizeY": fs.sizes["y"],
                "SizeC": fs.sizes["c"] if "c" in fs.sizes else 1,
                "SizeT": fs.sizes["t"] if "t" in fs.sizes else 1,
                "SizeZ": fs.sizes["z"] if "z" in fs.sizes else 1,
                "PhysicalSizeX": round(fs.calibration, 6)
                if fs.calibration
                else fs.calibration,
                "PhysicalSizeY": round(fs.calibration, 6)
                if fs.calibration
                else fs.calibration,
                "PhysicalSizeZ": round(fs.calibrationZ, 6)
                if fs.calibrationZ
                else fs.calibrationZ,
                # must be important to read pixels
                "pixel_type": fs.pixel_type,
                "PositionXYZ": pos,
            }
        )
    # not belonging to core md
    try:
        md["Date"] = fs.metadata.ImageAcquisitionDate(0)
    except AttributeError:
        md["Date"] = None
    tidy_metadata(md)
    return md, fs


def read_wrap(filepath, logpath="bioformats.log"):
    """Wrap for read function; capture standard output."""
    f = io.StringIO()
    with stdout_redirector(f):
        md, wr = read(filepath)
    out = f.getvalue()
    with open(logpath, "a") as f:
        f.write("\n\nreading " + filepath + "\n")
        f.write(out)
    return md, wr


def stitch(md, wrapper, c=0, t=0, z=0):
    """Stitch image tiles returning a tiled single plane."""
    xyz_list_of_sets = [p["PositionXYZ"] for p in md["series"]]
    if not all(len(p) == 1 for p in xyz_list_of_sets):
        msg = "One or more series doesn't have a single XYZ position."
        raise ValueError(msg)
    xy_positions = [next(iter(p))[:2] for p in xyz_list_of_sets]
    unique_x = np.sort(list({xy[0] for xy in xy_positions}))
    unique_y = np.sort(list({xy[1] for xy in xy_positions}))
    tiley = len(unique_y)
    tilex = len(unique_x)
    # tilemap only for complete tiles without None tile
    tilemap = np.zeros(shape=(tiley, tilex), dtype=int)
    for yi, y in enumerate(unique_y):
        for xi, x in enumerate(unique_x):
            indexes = [i for i, v in enumerate(xy_positions) if v == (x, y)]
            li = len(indexes)
            if li == 0:
                tilemap[yi, xi] = -1
            elif li == 1:
                tilemap[yi, xi] = indexes[0]
            else:
                msg = "Building tilemap failed in searching xy_position indexes."
                raise IndexError(msg)
    tiled_plane = np.zeros((md["SizeY"] * tiley, md["SizeX"] * tilex))
    for yt in range(tiley):
        for xt in range(tilex):
            if tilemap[yt, xt] >= 0:
                tiled_plane[
                    yt * md["SizeY"] : (yt + 1) * md["SizeY"],
                    xt * md["SizeX"] : (xt + 1) * md["SizeX"],
                ] = wrapper.read(c=c, t=t, z=z, series=tilemap[yt, xt], rescale=False)
    return tiled_plane


def diff(fp_a: str, fp_b: str) -> bool:
    """Diff for two image data.

    Parameters
    ----------
    fp_a : str
        File path for the first image.
    fp_b : str
        File path for the second image.

    Returns
    -------
    bool
        True if the two files are equal.
    """
    md_a, wr_a = read(fp_a)
    md_b, wr_b = read(fp_b)
    are_equal: bool = True

    # Check if metadata is equal
    are_equal = are_equal and (md_a == md_b)
    # MAYBE: print(md_b) maybe return md_a and different md_b
    if not are_equal:
        print("Metadata mismatch:")
        print("md_a:", md_a)
        print("md_b:", md_b)

    # Check pixel data equality
    if are_equal:
        for s in range(md_a["SizeS"]):
            for t in range(md_a["SizeT"]):
                for c in range(md_a["SizeC"]):
                    for z in range(md_a["SizeZ"]):
                        are_equal = are_equal and np.array_equal(
                            wr_a.read(series=s, t=t, c=c, z=z, rescale=False),
                            wr_b.read(series=s, t=t, c=c, z=z, rescale=False),
                        )

    return are_equal


def first_nonzero_reverse(llist: list[int]) -> None | int:
    """Return the index of the last nonzero element in a list.

    Parameters
    ----------
    llist : list[int]
        The input list of integers.

    Returns
    -------
    Optional[int]
        The index of the last nonzero element. Returns None if all elements are zero.

    Examples
    --------
    >>> first_nonzero_reverse([0, 2, 0, 0])
    -3
    >>> first_nonzero_reverse([0, 0, 0])
    None

    """
    for i in range(-1, -len(llist) - 1, -1):
        if llist[i] != 0:
            return i


def img_reader(
    filepath: str,
) -> tuple[bioformats.formatreader.ImageReader, javabridge.JClassWrapper]:
    """Initialize and return an ImageReader and its associated OMEXMLMetadata.

    Parameters
    ----------
    filepath : str
        The path to the image file.

    Returns
    -------
    tuple[bioformats.formatreader.ImageReader, javabridge.JClassWrapper]
        A tuple containing an ImageReader and its associated OMEXMLMetadata.

    Examples
    --------
    >>> image_reader, xml_metadata = img_reader("path/to/image.tif")
    >>> image_reader.getSizeX()
    # FIXME: look for an image to read.
    512
    >>> xml_metadata.getImageID()
    'Image:0'
    """
    ensure_vm()
    image_reader = bioformats.formatreader.make_image_reader_class()()
    image_reader.allowOpenToCheckType(True)
    # metadata a la java
    ome_xml_service = javabridge.JClassWrapper("loci.formats.services.OMEXMLService")
    service_factory = javabridge.JClassWrapper("loci.common.services.ServiceFactory")()
    service = service_factory.getInstance(ome_xml_service.klass)
    xml_md = service.createOMEXMLMetadata()
    image_reader.setMetadataStore(xml_md)
    image_reader.setId(filepath)
    return image_reader, xml_md


def read2(filepath, mdd_wanted=False):
    """Read a data using bioformats through javabridge (as for read).

    Get all OME metadata.

    """
    image_reader, xml_md = img_reader(filepath)
    # sr = image_reader.getSeriesCount()
    md, mdd = get_md_dict(xml_md, filepath)
    md["Format"] = image_reader.getFormat()
    # Make a fake ImageReader and install the one above inside it
    wrapper = bioformats.formatreader.ImageReader(path=filepath, perform_init=False)
    wrapper.rdr = image_reader
    if mdd_wanted:
        return md, wrapper, mdd
    else:
        return md, wrapper


global loci


def start_jpype(java_memory: str = "512m") -> None:
    """Start the JPype JVM with the specified Java memory.

    Parameters
    ----------
    java_memory : str, optional
        The amount of Java memory to allocate, e.g., "512m" (default is "512m").

    """
    # loci_path = _find_jar()  # Uncomment or adjust as needed
    loci_path = "/home/dan/workspace/loci_tools.jar"  # Adjust the path as needed
    jpype.startJVM(
        jpype.getDefaultJVMPath(),
        "-ea",
        "-Djava.class.path=" + loci_path,
        "-Xmx" + java_memory,
    )
    log4j = jpype.JPackage("org.apache.log4j")
    log4j.BasicConfigurator.configure()
    log4j_logger = log4j.Logger.getRootLogger()
    log4j_logger.setLevel(log4j.Level.ERROR)


def read_jpype(
    filepath: str, java_memory: str = "512m"
) -> tuple[dict[str, Any], tuple[jpype.JObject, str, dict[str, Any]]]:
    """Read metadata and data from an image file using JPype.

    Get all OME metadata.

    rdr as a lot of information e.g rdr.isOriginalMetadataPopulated() (core,
    OME, original metadata)

    This function uses JPype to read metadata and data from an image file. It
    returns a dictionary containing tidied metadata and a tuple containing
    JPype objects for the ImageReader, data type, and additional metadata.

    Parameters
    ----------
    filepath : str
        The path to the image file.
    java_memory : str, optional
        The amount of Java memory to allocate (default is "512m").

    Returns
    -------
    Tuple[dict, Tuple[jpype.JObject, str, dict]]
        A tuple containing:
        - A dictionary with tidied metadata.
        - A tuple containing JPype objects:
            - ImageReader: JPype object for reading the image.
            - dtype: Data type of the image data.
            - additional_metadata: Additional metadata from JPype.

    Examples
    --------
    >>> metadata, jpype_objects = read_jpype("path/to/image.tif")
    >>> metadata["SizeX"]
    # FIXME: find data file.
    512
    >>> jpype_objects[1]
    'u2'
    """
    # Start java VM and initialize logger (globally)
    if not jpype.isJVMStarted():
        start_jpype(java_memory)

    if not jpype.isThreadAttachedToJVM():
        jpype.attachThreadToJVM()

    loci = jpype.JPackage("loci")
    # rdr = loci.formats.ChannelSeparator(loci.formats.ChannelFiller())
    rdr = loci.formats.ImageReader()
    rdr.setMetadataStore(loci.formats.MetadataTools.createOMEXMLMetadata())
    rdr.setId(filepath)
    xml_md = rdr.getMetadataStore()
    # sr = image_reader.getSeriesCount()
    md, mdd = get_md_dict(xml_md, filepath)
    # md['Format'] = rdr.format
    # new core_md
    # core_md = init_metadata(md["ImageCount"][0][1], rdr.format)
    core_md = init_metadata(md["ImageCount"][0][1], rdr.getFormat())
    for s in range(core_md["SizeS"]):
        # fs = pims.Bioformats(filepath, series=s)
        try:
            pos = {
                md["PlanePositionX"][s][1][0],
                md["PlanePositionY"][s][1][0],
                # for index error of Z
                md["PlanePositionZ"][0][1][0],
            }
        except (AttributeError, KeyError):
            pos = None
        try:
            psx = round(md["PixelsPhysicalSizeX"][0][1][0], 6)
        except KeyError:
            psx = None
        try:
            psy = round(md["PixelsPhysicalSizeY"][0][1][0], 6)
        except KeyError:
            psy = None
        try:
            psz = round(md["PixelsPhysicalSizeZ"][0][1][0], 6)
        except KeyError:
            psz = None
        core_md["series"].append(
            {
                "SizeX": md["PixelsSizeX"][0][1],
                "SizeY": md["PixelsSizeY"][0][1],
                "SizeC": md["PixelsSizeC"][0][1],
                "SizeT": md["PixelsSizeT"][0][1],
                "SizeZ": [v[1] for v in md["PixelsSizeZ"]]
                if len(md["PixelsSizeZ"]) > 1
                else md["PixelsSizeZ"][0][1],
                "PhysicalSizeX": psx,
                "PhysicalSizeY": psy,
                "PhysicalSizeZ": psz,
                # must be important to read pixels
                "pixel_type": md["PixelsType"][0][1],
                "PositionXYZ": pos,
            }
        )
    # not belonging to core md
    try:
        core_md["Date"] = md["ImageAcquisitionDate"][0][1]
    except (KeyError, AttributeError):
        core_md["Date"] = None
    tidy_metadata(core_md)
    # new: finish here
    # Checkout reader dtype and define read mode
    is_little_endian = rdr.isLittleEndian()
    le_prefix = [">", "<"][is_little_endian]
    format_tools = loci.formats.FormatTools
    _dtype_dict = {
        format_tools.INT8: "i1",
        format_tools.UINT8: "u1",
        format_tools.INT16: le_prefix + "i2",
        format_tools.UINT16: le_prefix + "u2",
        format_tools.INT32: le_prefix + "i4",
        format_tools.UINT32: le_prefix + "u4",
        format_tools.FLOAT: le_prefix + "f4",
        format_tools.DOUBLE: le_prefix + "f8",
    }
    _dtype_dict_java = {}
    for loci_format in _dtype_dict:
        _dtype_dict_java[loci_format] = (
            format_tools.getBytesPerPixel(loci_format),
            format_tools.isFloatingPoint(loci_format),
            is_little_endian,
        )
    # determine pixel type
    pixel_type = rdr.getPixelType()
    dtype = _dtype_dict[pixel_type]
    return core_md, (rdr, dtype, md)
    # # Make a fake ImageReader and install the one above inside it
    # wrapper = bioformats.formatreader.ImageReader(
    #     path=filepath, perform_init=False)
    # wrapper.rdr = rdr

    # if mdd_wanted:
    #     return md, wrapper, mdd
    # else:
    #     return md, wrapper


class FoundMetadataError(Exception):
    """Exception raised when metadata is found during a specific condition."""

    pass


def get_md_dict(
    xml_md, filepath: Optional[str] = None, debug: bool = False
) -> tuple[dict[str, Any], dict[str, str]]:
    """Parse xml_md and return parsed md dictionary and md status dictionary.

    Parameters
    ----------
    xml_md: Any
        The xml metadata to parse.
    filepath: str, optional
        The filepath, used for logging JavaExceptions.
    debug: bool, optional
        Debugging flag.

    Returns
    -------
    Tuple[Dict[str, Any], Dict[str, str]]
        A tuple containing:
        - md: dict
            Parsed metadata dictionary excluding None values.
        - mdd: dict
            Metadata status dictionary indicating if a value was found ('Found'),
            is None ('None'), or if there was a JavaException ('Jmiss').

    Raises
    ------
    FoundMetadataError:
        If metadata is found during a specific condition.

    """
    keys = [
        # xml_md.__dir__() proved more robust than xml_md.methods
        m
        for m in xml_md.__dir__()
        if m[:3] == "get"
        and m
        not in (
            "getRoot",
            "getClass",
            "getXMLAnnotationValue",
            "getPixelsBinDataBigEndian",
        )
    ]
    md = {}
    mdd = {}
    if filepath:
        javaexception_logfile = open(filepath + ".mmdata.log", "w")
    for k in keys:
        try:
            for npar in range(5):
                try:
                    t = (0,) * npar
                    v = getattr(xml_md, k)(*t)
                    raise FoundMetadataError()
                except (TypeError, RuntimeError):
                    continue
        except FoundMetadataError:
            if v is not None:
                # md[k] = [(npar, conversion(v))] # to get only the first value
                md[k[3:]] = get_allvalues_grouped(xml_md, k, npar, debug=debug)
                mdd[k] = "Found"
            else:
                # md[k[3:]] = None
                # md[k[3:]] = get_allvalues_grouped(xml_md, k, npar)
                mdd[k] = "None"
            # keys.remove(k)
        except Exception as e:
            if filepath:
                javaexception_logfile.write(str((k, type(e), e, "--", npar)) + "\n")
            mdd[k] = "Jmiss"
            continue
    if filepath:
        javaexception_logfile.close()
    return md, mdd


class JavaField(Protocol):
    """Define a Protocol for JavaField."""

    def value(self) -> None | str | float | int:
        """Get the value of the JavaField.

        Returns
        -------
        None | str | float | int:
            The value of the JavaField, which can be None or one of the specified types.
        """
        ...


# Type for values in your metadata
MDValueType = str | bool | int | float
MDJavaFieldType = None | MDValueType | JavaField


def convert_java_numeric_field(
    java_field: MDJavaFieldType,
) -> MDValueType | None:
    """Convert numeric fields from Java.

    The input `java_field` can be None. It can happen for a list of values that
    doesn't start with None, e.g., (.., ((4, 1), (543.0, 'nm')), ((4, 2), None).

    Parameters
    ----------
    java_field: None | str | float | int
        A numeric field from Java.

    Returns
    -------
    None | str | float | int:
        The converted number as int or float types, or None.

    Raises
    ------
    ValueError:
        On non-numeric input.

    Notes
    -----
    This is necessary because getDouble, getFloat are not
    reliable ('0.9' becomes 0.89999).

    """
    if java_field is None:
        return None
    snum = str(java_field)
    try:
        return int(snum)
    except ValueError:
        try:
            return float(snum)
        except ValueError:
            # If the value is a string but not convertible to int or float,
            # return the original string.
            return snum


def convert_value(
    v: JavaField, debug: bool = False
) -> JavaField | tuple[JavaField, type, str]:
    """Convert value from Instance of loci.formats.ome.OMEXMLMetadataImpl."""
    if type(v) in {str, bool, int}:
        md2 = v, type(v), "v"
    elif hasattr(v, "getValue"):
        vv = v.getValue()
        if type(vv) in {str, bool, int, float}:
            md2 = vv, type(vv), "gV"
        else:
            vv = convert_java_numeric_field(vv)
            md2 = vv, type(vv), "gVc"
    elif hasattr(v, "unit"):
        # this conversion is better than using stringIO
        vv = convert_java_numeric_field(v.value()), v.unit().getSymbol()
        md2 = vv, type(vv), "unit"
    else:
        try:
            vv = convert_java_numeric_field(v)
            md2 = vv, type(vv), "c"
        except ValueError as ve:
            # Issue a warning for ValueError
            warnings.warn(f"ValueError: {ve}", category=UserWarning, stacklevel=2)
            md2 = v, type(v), "un"
        except Exception as e:
            # Issue a warning for other exceptions
            warnings.warn(
                f"EXCEPTION: {type(e).__name__}: {e}",
                category=UserWarning,
                stacklevel=2,
            )
            md2 = v, type(v), "un"  # Probably useless
            raise  # Reraise the exception for further analysis
    if debug:
        return md2
    else:
        return md2[0]


class StopExceptionError(Exception):
    """Exception raised when need to stop."""

    pass


def next_tuple(llist, s: bool):
    """Generate the next tuple in lexicographical order.

    # FIXME: strange math

    Parameters
    ----------
    llist : list
        The input list representing a tuple.
    s : bool
        A flag indicating whether to increment the last element or not.

    Returns
    -------
    list:
        The next tuple in lexicographical order.

    Raises
    ------
    StopExceptionError:
        If the input tuple is empty or if the generation needs to stop.
    """
    # Next item never exists for an empty tuple.
    if len(llist) == 0:
        raise StopExceptionError
    if s:
        llist[-1] += 1
    else:
        idx = first_nonzero_reverse(llist)
        if idx == -len(llist):
            raise StopExceptionError
        else:
            llist[idx] = 0
            llist[idx - 1] += 1
    return llist


def get_allvalues_grouped(
    metadata, k: str, npar: int, debug: bool = False
) -> list[tuple[tuple[int, ...], Any]]:
    """Retrieve and group metadata values for a given key.

    Parameters
    ----------
    metadata:
        The metadata object.
    k : str
        The key for which values are retrieved.
    npar : int
        The number of parameters for the key.
    debug : bool, optional
        Flag to enable debug mode.

    Returns
    -------
    List[Tuple[Tuple[int, ...], Any]]:
        A list of tuples containing the tuple configuration and corresponding values.

    Raises
    ------
    StopExceptionError:
        If the generation needs to stop.
    """
    res = []
    ll = [0] * npar
    t = tuple(ll)
    v = convert_value(getattr(metadata, k)(*t), debug=debug)
    res.append((t, v))
    s = True
    while True:
        try:
            ll = next_tuple(ll, s)
            t = tuple(ll)
            v = convert_value(getattr(metadata, k)(*t), debug=debug)
            res.append((t, v))
            s = True
        except StopExceptionError:
            break
        except Exception:
            s = False
    # tidy up common metadata
    # TODO Separate into a function to be tested on sample metadata pr what?
    if len(res) > 1:
        ll = [e[1] for e in res]
        if ll.count(ll[0]) == len(res):
            res = [res[-1]]
        elif len(res[0][0]) >= 2:
            # first group the list of tuples by (tuple_idx=0)
            grouped_res = collections.defaultdict(list)
            for t, v in res:
                grouped_res[t[0]].append(v)
            max_key = max(grouped_res.keys())  # or: res[-1][0][0]
            # now check for single common value within a group
            new_res = []
            for k, v in grouped_res.items():
                if v.count(v[0]) == len(v):
                    new_res.append(((k, len(v) - 1), v[-1]))
            if new_res:
                res = new_res
            # now check for the same group repeated
            for _, v in grouped_res.items():
                if v != grouped_res[max_key]:
                    break
            else:
                # This block executes if the loop completes without a 'break'
                res = res[-len(v) :]
    return res
