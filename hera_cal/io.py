# -*- coding: utf-8 -*-
# Copyright 2018 the HERA Project
# Licensed under the MIT License

import numpy as np
from pyuvdata import UVCal, UVData
from pyuvdata import utils as uvutils
from collections import OrderedDict as odict
from hera_cal.datacontainer import DataContainer
import hera_cal as hc
import operator
import os
import copy
import warnings
from functools import reduce
from hera_cal.utils import polnum2str, polstr2num, jnum2str, jstr2num
from hera_cal.utils import split_pol, conj_pol
import collections


class HERACal(UVCal):
    '''HERACal is a subclass of pyuvdata.UVCal meant to serve as an interface between
    pyuvdata-readable calfits files and dictionaries (the in-memory format for hera_cal)
    that map antennas and polarizations to gains, flags, and qualities. Supports standard
    UVCal functionality, along with read() and update() functionality for going back and
    forth to dictionaires. Upon read(), stores useful metadata internally.

    Does not support partial data loading or writing. Assumes a single spectral window.
    '''

    def __init__(self, input_cal):
        '''Instantiate a HERACal object. Currently only supports calfits files.

        Arguments:
            input_cal: string calfits file path or list of paths
        '''
        super(HERACal, self).__init__()

        # parse input_data as filepath(s)
        if isinstance(input_cal, str):
            self.filepaths = [input_cal]
        elif isinstance(input_cal, collections.Iterable):  # List loading
            if np.all([isinstance(i, str) for i in input_cal]):  # List of visibility data paths
                self.filepaths = list(input_cal)
            else:
                raise TypeError('If input_cal is a list, it must be a list of strings.')
        else:
            raise ValueError('input_cal must be a string or a list of strings.')

    def _extract_metadata(self):
        '''Extract and store useful metadata and array indexing dictionaries.'''
        self.freqs = np.unique(self.freq_array)
        self.times = np.unique(self.time_array)
        self.pols = [jnum2str(j) for j in self.jones_array]
        self._jnum_indices = {jnum: i for i, jnum in enumerate(self.jones_array)}
        self.ants = [(ant, pol) for ant in self.ant_array for pol in self.pols]
        self._antnum_indices = {ant: i for i, ant in enumerate(self.ant_array)}

    def build_calcontainers(self):
        '''Turns the calibration information currently loaded into the HERACal object
        into ordered dictionaries that map antenna-pol tuples to calibration waterfalls.
        Computes and stores internally useful metadata in the process.

        Returns:
            gains: dict mapping antenna-pol keys to (Nint, Nfreq) complex gains arrays
            flags: dict mapping antenna-pol keys to (Nint, Nfreq) boolean flag arrays
            quals: dict mapping antenna-pol keys to (Nint, Nfreq) float qual arrays
            total_qual: dict mapping polarization to (Nint, Nfreq) float total quality array
        '''
        self._extract_metadata()
        gains, flags, quals, total_qual = odict(), odict(), odict(), odict()

        # build dict of gains, flags, and quals
        for (ant, pol) in self.ants:
            i, ip = self._antnum_indices[ant], self._jnum_indices[jstr2num(pol)]
            gains[(ant, pol)] = np.array(self.gain_array[i, 0, :, :, ip].T)
            flags[(ant, pol)] = np.array(self.flag_array[i, 0, :, :, ip].T)
            quals[(ant, pol)] = np.array(self.quality_array[i, 0, :, :, ip].T)

        # build dict of total_qual if available
        for pol in self.pols:
            ip = self._jnum_indices[jstr2num(pol)]
            if self.total_quality_array is not None:
                total_qual[pol] = np.array(self.total_quality_array[0, :, :, ip].T)
            else:
                total_qual = None

        return gains, flags, quals, total_qual

    def read(self):
        '''Reads calibration information from file, computes useful metadata and returns
        dictionaries that map antenna-pol tuples to calibration waterfalls.

        Returns:
            gains: dict mapping antenna-pol keys to (Nint, Nfreq) complex gains arrays
            flags: dict mapping antenna-pol keys to (Nint, Nfreq) boolean flag arrays
            quals: dict mapping antenna-pol keys to (Nint, Nfreq) float qual arrays
            total_qual: dict mapping polarization to (Nint, Nfreq) float total quality array
        '''
        self.read_calfits(self.filepaths)
        return self.build_calcontainers()

    def update(self, gains=None, flags=None, quals=None, total_qual=None):
        '''Update internal calibrations arrays (data_array, flag_array, and nsample_array)
        using DataContainers (if not left as None) in preparation for writing to disk.

        Arguments:
            gains: optional dict mapping antenna-pol to complex gains arrays
            flags: optional dict mapping antenna-pol to boolean flag arrays
            quals: optional dict mapping antenna-pol to float qual arrays
            total_qual: optional dict mapping polarization to float total quality array
        '''
        # loop over and update gains, flags, and quals
        data_arrays = [self.gain_array, self.flag_array, self.quality_array]
        for to_update, array in zip([gains, flags, quals], data_arrays):
            if to_update is not None:
                for (ant, pol) in to_update.keys():
                    i, ip = self._antnum_indices[ant], self._jnum_indices[jstr2num(pol)]
                    array[i, 0, :, :, ip] = to_update[(ant, pol)].T

        # update total_qual
        if total_qual is not None:
            for pol in total_qual.keys():
                ip = self._jnum_indices[jstr2num(pol)]
                self.total_quality_array[0, :, :, ip] = total_qual[pol].T


class HERAData(UVData):
    '''HERAData is a subclass of pyuvdata.UVData meant to serve as an interface between
    pyuvdata-compatible data formats on disk (especially uvh5) and DataContainers,
    the in-memory format for visibilities used in hera_cal. In addition to standard
    UVData functionality, HERAData supports read() and update() functions that interface
    between internal UVData data storage and DataContainers, which contain visibility
    data in a dictionary-like format, along with some useful metadata. read() supports
    partial data loading, though only the most useful subset of selection modes from
    pyuvdata (and not all modes for all data types).

    When using uvh5, HERAData supports additional useful functionality:
    * Upon __init__(), the most useful metadata describing the entire file is loaded into
      the object (everything in HERAData_metas; see get_metadata_dict() for details).
    * Partial writing using partial_write(), which will initialize a new file with the
      same metadata and write to disk using DataContainers by assuming that the user is
      writing to the same part of the data as the most recent read().
    * Generators that enable iterating over baseline, frequency, or time in chunks (see
      iterate_over_bls(), iterate_over_freqs(), and iterate_over_times() for details).

    Assumes a single spectral window. Assumes that data for a given baseline is regularly
    spaced in the underlying data_array.
    '''

    # static list of useful metadata to calculate and save
    HERAData_metas = ['ants', 'antpos', 'freqs', 'times', 'lsts', 'pols',
                      'antpairs', 'bls', 'times_by_bl', 'lsts_by_bl']

    def __init__(self, input_data, filetype='uvh5'):
        '''Instantiate a HERAData object. If the filetype == uvh5, read in and store
        useful metadata (see get_metadata_dict()), either as object attributes or,
        if input_data is a list, as dictionaries mapping string paths to metadata.

        Arguments:
            input_data: string data file path or list of string data file paths
            filetype: supports 'uvh5' (defualt), 'miriad', 'uvfits'
        '''
        # initialize as empty UVData object
        super(HERAData, self).__init__()

        # parse input_data as filepath(s)
        if isinstance(input_data, str):
            self.filepaths = [input_data]
        elif isinstance(input_data, collections.Iterable):  # List loading
            if np.all([isinstance(i, str) for i in input_data]):  # List of visibility data paths
                self.filepaths = list(input_data)
            else:
                raise TypeError('If input_data is a list, it must be a list of strings.')
        else:
            raise ValueError('input_data must be a string or a list of strings.')
        for f in self.filepaths:
            if not os.path.exists(f):
                raise IOError('Cannot find file ' + f)

        # load metadata from file
        self.filetype = filetype
        if self.filetype == 'uvh5':
            # read all UVData metadata from first file
            temp_paths = copy.deepcopy(self.filepaths)
            self.filepaths = self.filepaths[0]
            self.read(read_data=False)
            self.filepaths = temp_paths

            if len(self.filepaths) > 1:  # save HERAData_metas in dicts
                for meta in self.HERAData_metas:
                    setattr(self, meta, {})
                for path in self.filepaths:
                    hc = HERAData(path, filetype='uvh5')
                    meta_dict = self.get_metadata_dict()
                    for meta in self.HERAData_metas:
                        getattr(self, meta)[path] = meta_dict[meta]
            else:  # save HERAData_metas as attributes
                self._writers = {}
                for key, value in self.get_metadata_dict().items():
                    setattr(self, key, value)

        elif self.filetype in ['miriad', 'uvfits']:
            for meta in self.HERAData_metas:
                setattr(self, meta, None)  # no pre-loading of metadata
        else:
            raise NotImplementedError('Filetype ' + self.filetype + ' has not been implemented.')

    def reset(self):
        '''Resets all standard UVData attributes, potentially freeing memory.'''
        super(HERAData, self).__init__()

    def get_metadata_dict(self):
        ''' Produces a dictionary of the most useful metadata. Used as object
        attributes and as metadata to store in DataContainers.

        Returns:
            metadata_dict: dictionary of all items in self.HERAData_metas
        '''
        antpos, ants = self.get_ENU_antpos()
        antpos = odict(zip(ants, antpos))

        freqs = np.unique(self.freq_array)
        times = np.unique(self.time_array)
        lst_indices = np.unique(self.lst_array.ravel(), return_index=True)[1]
        lsts = self.lst_array.ravel()[np.sort(lst_indices)]
        pols = [polnum2str(polnum) for polnum in self.polarization_array]
        antpairs = self.get_antpairs()
        bls = [antpair + (pol,) for antpair in antpairs for pol in pols]

        times_by_bl = {antpair: np.array(self.time_array[self._blt_slices[antpair]])
                       for antpair in antpairs}
        lsts_by_bl = {antpair: np.array(self.lst_array[self._blt_slices[antpair]])
                      for antpair in antpairs}

        locs = locals()
        return {meta: locs[meta] for meta in self.HERAData_metas}

    def _determine_blt_slicing(self):
        '''Determine the mapping between antenna pairs and
        slices of the blt axis of the data_array.'''
        self._blt_slices = {}
        for ant1, ant2 in self.get_antpairs():
            indices = self.antpair2ind(ant1, ant2)
            if len(indices) == 1:  # only one blt matches
                self._blt_slices[(ant1, ant2)] = slice(indices[0], indices[0] + 1, self.Nblts)
            elif not (len(set(np.ediff1d(indices))) == 1):  # checks if the consecutive differences are all the same
                raise NotImplementedError('UVData objects with non-regular spacing of '
                                          'baselines in its baseline-times are not supported.')
            else:
                self._blt_slices[(ant1, ant2)] = slice(indices[0], indices[-1] + 1,
                                                       indices[1] - indices[0])

    def _determine_pol_indexing(self):
        '''Determine the mapping between polnums and indices
        in the polarization axis of the data_array.'''
        self._polnum_indices = {}
        for i, polnum in enumerate(self.polarization_array):
            self._polnum_indices[polnum] = i

    def _get_slice(self, data_array, key):
        '''Return a copy of the Nint by Nfreq waterfall or waterfalls for a given key. Abstracts
        away both baseline ordering (by applying complex conjugation) and polarization capitalization.

        Arguments:
            data_array: numpy array of shape (Nblts, 1, Nfreq, Npol), i.e. the size of the full data.
                One generally uses this object's own self.data_array, self.flag_array, or self.nsample_array.
            key: if of the form (0,1,'xx'), return anumpy array.
                 if of the form (0,1), return a dict mapping pol strings to waterfalls.
                 if of of the form 'xx', return a dict mapping ant-pair tuples to waterfalls.
        '''
        if isinstance(key, str):  # asking for a pol
            return {antpair: self._get_slice(data_array, antpair + (key,)) for antpair in self.get_antpairs()}
        elif len(key) == 2:  # asking for antpair
            pols = np.array([polnum2str(polnum) for polnum in self.polarization_array])
            return {pol: self._get_slice(data_array, key + (pol,)) for pol in pols}
        elif len(key) == 3:  # asking for bl-pol
            try:
                return np.array(data_array[self._blt_slices[tuple(key[0:2])], 0, :,
                                           self._polnum_indices[polstr2num(key[2])]])
            except KeyError:
                return np.conj(data_array[self._blt_slices[tuple(key[1::-1])], 0, :,
                                          self._polnum_indices[polstr2num(conj_pol(key[2]))]])
        else:
            raise KeyError('Unrecognized key type for slicing data.')

    def _set_slice(self, data_array, key, value):
        '''Update data_array with Nint by Nfreq waterfall(s). Abstracts away both baseline
        ordering (by applying complex conjugation) and polarization capitalization.

        Arguments:
            data_array: numpy array of shape (Nblts, 1, Nfreq, Npol), i.e. the size of the full data.
                One generally uses this object's own self.data_array, self.flag_array, or self.nsample_array.
            key: baseline (e.g. (0,1,'xx)), ant-pair tuple (e.g. (0,1)), or pol str (e.g. 'xx')
            value: if key is a baseline, must be an (Nint, Nfreq) numpy array;
                   if key is an ant-pair tuple, must be a dict mapping pol strings to waterfalls;
                   if key is a pol str, must be a dict mapping ant-pair tuples to waterfalls
        '''
        if isinstance(key, str):  # providing pol with all antpairs
            for antpair in value.keys():
                self._set_slice(data_array, (antpair + (key,)), value[antpair])
        elif len(key) == 2:  # providing antpair with all pols
            for pol in value.keys():
                self._set_slice(data_array, (key + (pol,)), value[pol])
        elif len(key) == 3:  # providing bl-pol
            try:
                data_array[self._blt_slices[tuple(key[0:2])], 0, :,
                           self._polnum_indices[polstr2num(key[2])]] = value
            except(KeyError):
                data_array[self._blt_slices[tuple(key[1::-1])], 0, :,
                           self._polnum_indices[polstr2num(conj_pol(key[2]))]] = np.conj(value)
        else:
            raise KeyError('Unrecognized key type for slicing data.')

    def build_datacontainers(self):
        '''Turns the data currently loaded into the HERAData object into DataContainers.
        Returned DataContainers include useful metadata specific to the data actually
        in the DataContainers (which may be a subset of the total data). This includes
        antenna positions, frequencies, all times, all lsts, and times and lsts by baseline.

        Returns:
            data: DataContainer mapping baseline keys to complex visibility waterfalls
            flags: DataContainer mapping baseline keys to boolean flag waterfalls
            nsamples: DataContainer mapping baseline keys to interger Nsamples waterfalls
        '''
        # build up DataContainers
        data, flags, nsamples = odict(), odict(), odict()
        meta = self.get_metadata_dict()
        for bl in meta['bls']:
            data[bl] = self._get_slice(self.data_array, bl)
            flags[bl] = self._get_slice(self.flag_array, bl)
            nsamples[bl] = self._get_slice(self.nsample_array, bl)
        data = DataContainer(data)
        flags = DataContainer(flags)
        nsamples = DataContainer(nsamples)

        # store useful metadata inside the DataContainers
        for dc in [data, flags, nsamples]:
            for attr in ['antpos', 'freqs', 'times', 'lsts', 'times_by_bl', 'lsts_by_bl']:
                setattr(dc, attr, meta[attr])

        return data, flags, nsamples

    def read(self, bls=None, polarizations=None, times=None,
             frequencies=None, freq_chans=None, read_data=True):
        '''Reads data from file. Supports partial data loading. Default: read all data in file.

        Arguments:
            bls: A list of antenna number tuples (e.g. [(0,1), (3,2)]) or a list of
                baseline 3-tuples (e.g. [(0,1,'xx'), (2,3,'yy')]) specifying baselines
                to keep in the object. For length-2 tuples, the  ordering of the numbers
                within the tuple does not matter. For length-3 tuples, the polarization
                string is in the order of the two antennas. If length-3 tuples are provided,
                the polarizations argument below must be None. Ignored if read_data is False.
            polarizations: The polarizations to include when reading data into
                the object.  Ignored if read_data is False.
            times: The times to include when reading data into the object.
                Ignored if read_data is False. Miriad will load then select on this axis.
            frequencies: The frequencies to include when reading data. Ignored if read_data
                is False. Miriad will load then select on this axis.
            freq_chans: The frequency channel numbers to include when reading data. Ignored
                if read_data is False. Miriad will load then select on this axis.
            read_data: Read in the visibility and flag data. If set to false, only the
                basic metadata will be read in and nothing will be returned. Results in an
                incompletely defined object (check will not pass). Default True.

        Returns:
            data: DataContainer mapping baseline keys to complex visibility waterfalls
            flags: DataContainer mapping baseline keys to boolean flag waterfalls
            nsamples: DataContainer mapping baseline keys to interger Nsamples waterfalls
        '''
        # save last read parameters
        locs = locals()
        partials = ['bls', 'polarizations', 'times', 'frequencies', 'freq_chans']
        self.last_read_kwargs = {p: locs[p] for p in partials}

        # load data
        if self.filetype == 'uvh5':
            self.read_uvh5(self.filepaths, bls=bls, polarizations=polarizations, times=times,
                           frequencies=frequencies, freq_chans=freq_chans, read_data=read_data)
        else:
            if not read_data:
                raise NotImplementedError('reading only metadata is not implemented for' + self.filetype)
            if self.filetype == 'miriad':
                self.read_miriad(self.filepaths, bls=bls, polarizations=polarizations)
                if any([times is not None, frequencies is not None, freq_chans is not None]):
                    warnings.warn('miriad does not support partial loading for times and frequencies. '
                                  'Loading the file first and then performing select.')
                    self.select(times=times, frequencies=frequencies, freq_chans=freq_chans)
            elif self.filetype == 'uvfits':
                self.read_uvfits(self.filepaths, bls=bls, polarizations=polarizations,
                                 times=times, frequencies=frequencies, freq_chans=freq_chans)
                self.unphase_to_drift()

        # process data into DataContainers
        if read_data or self.filetype == 'uvh5':
            self._determine_blt_slicing()
            self._determine_pol_indexing()
        if read_data:
            return self.build_datacontainers()

    def __getitem__(self, key):
        '''Shortcut for reading a single visibility waterfall given a baseline tuple.'''
        return self.read(bls=key)[0][key]

    def update(self, data=None, flags=None, nsamples=None):
        '''Update internal data arrays (data_array, flag_array, and nsample_array)
        using DataContainers (if not left as None) in preparation for writing to disk.

        Arguments:
            data: Optional DataContainer mapping baselines to complex visibility waterfalls
            flags: Optional DataContainer mapping baselines to boolean flag waterfalls
            nsamples: Optional DataContainer mapping baselines to interger Nsamples waterfalls
        '''
        if data is not None:
            for bl in data.keys():
                self._set_slice(self.data_array, bl, data[bl])
        if flags is not None:
            for bl in flags.keys():
                self._set_slice(self.flag_array, bl, flags[bl])
        if nsamples is not None:
            for bl in nsamples.keys():
                self._set_slice(self.nsample_array, bl, nsamples[bl])

    def partial_write(self, output_path, data=None, flags=None, nsamples=None, clobber=False, inplace=False, add_to_history='', **kwargs):
        '''Writes part of a uvh5 file using DataContainers whose shape matches the most recent
        call to HERAData.read() in this object. The overall file written matches the shape of the
        input_data file called on __init__. Any data/flags/nsamples left as None will be written
        as was currently stored in the HERAData object. Does not work for other filetypes or when
        the HERAData object is initialized with a list of files.

        Arguments:
            output_path: path to file to write uvh5 file to
            data: Optional DataContainer mapping baselines to complex visibility waterfalls
            flags: Optional DataContainer mapping baselines to boolean flag waterfalls
            nsamples: Optional DataContainer mapping baselines to interger Nsamples waterfalls
            clobber: if True, overwrites existing file at output_path
            inplace: update this object's data_array, flag_array, and nsamples_array.
                This saves memory but alters the HERAData object.
            add_to_history: string to append to history (only used on first call of
                partial_write for a given output_path)
            kwargs: addtional keyword arguments update UVData attributes. (Only used on
                first call of partial write for a given output_path).
        '''
        # Type verifications
        if self.filetype != 'uvh5':
            raise NotImplementedError('Partial writing for filetype ' + self.filetype + ' has not been implemented.')
        if len(self.filepaths) > 1:
            raise NotImplementedError('Partial writing for list-loaded HERAData objects has not been implemented.')

        # get writer or initialize new writer if necessary
        if output_path in self._writers:
            hd_writer = self._writers[output_path]  # This hd_writer has metadata for the entire output file
        else:
            hd_writer = HERAData(self.filepaths[0])
            hd_writer.history += add_to_history
            for attribute, value in kwargs.items():
                hd_writer.__setattr__(attribute, value)
            hd_writer.initialize_uvh5_file(output_path, clobber=clobber)  # Makes an empty file (called only once)
            self._writers[output_path] = hd_writer

        if inplace:  # update this objects's arrays using DataContainers
            this = self
        else:  # make a copy of this object and then update the relevant arrays using DataContainers
            this = copy.deepcopy(self)
        this.update(data=data, flags=flags, nsamples=nsamples)
        hd_writer.write_uvh5_part(output_path, this.data_array, this.flag_array,
                                  this.nsample_array, **self.last_read_kwargs)

    def iterate_over_bls(self, Nbls=1, bls=None):
        '''Produces a generator that iteratively yields successive calls to
        HERAData.read() by baseline or group of baselines.

        Arguments:
            Nbls: number of baselines to load at once.
            bls: optional user-provided list of baselines to iterate over.
                Default: use self.bls (which only works for uvh5).

        Yields:
            data, flags, nsamples: DataContainers (see HERAData.read() for more info).
        '''
        if bls is None:
            if self.filetype != 'uvh5':
                raise NotImplementedError('Baseline iteration without explicitly setting bls for filetype ' + self.filetype
                                          + ' without setting bls has not been implemented.')
            bls = self.bls
            if isinstance(bls, dict):  # multiple files
                bls = list(set([bl for bls in bls.values() for bl in bls]))
            bls = sorted(bls)
        for i in range(0, len(bls), Nbls):
            yield self.read(bls=bls[i:i + Nbls])

    def iterate_over_freqs(self, Nchans=1, freqs=None):
        '''Produces a generator that iteratively yields successive calls to
        HERAData.read() by frequency channel or group of contiguous channels.

        Arguments:
            Nchans: number of frequencies to load at once.
            freqs: optional user-provided list of frequencies to iterate over.
                Default: use self.freqs (which only works for uvh5).

        Yields:
            data, flags, nsamples: DataContainers (see HERAData.read() for more info).
        '''
        if freqs is None:
            if self.filetype != 'uvh5':
                raise NotImplementedError('Frequency iteration for filetype ' + self.filetype
                                          + ' without setting freqs has not been implemented.')
            freqs = self.freqs
            if isinstance(self.freqs, dict):  # multiple files
                freqs = np.unique(self.freqs.values())
        for i in range(0, len(freqs), Nchans):
            yield self.read(frequencies=freqs[i:i + Nchans])

    def iterate_over_times(self, Nints=1, times=None):
        '''Produces a generator that iteratively yields successive calls to
        HERAData.read() by time or group of contiguous times.

        Arguments:
            Nints: number of integrations to load at once.
            times: optional user-provided list of times to iterate over.
                Default: use self.times (which only works for uvh5).

        Yields:
            data, flags, nsamples: DataContainers (see HERAData.read() for more info).
        '''
        if times is None:
            if self.filetype != 'uvh5':
                raise NotImplementedError('Time iteration for filetype ' + self.filetype
                                          + ' without setting times has not been implemented.')
            times = self.times
            if isinstance(times, dict):  # multiple files
                times = np.unique(times.values())
        for i in range(0, len(times), Nints):
            yield self.read(times=times[i:i + Nints])


#######################################################################
#                             LEGACY CODE
#######################################################################


def to_HERAData(input_data, filetype='miriad'):
    '''Converts a string path, UVData, or HERAData object, or a list of any one of those, to a
    single HERAData object without loading any new data.

    Arguments:
        input_data: data file path, or UVData/HERAData instance, or list of either strings of data
            file paths or list of UVData/HERAData instances to combine into a single HERAData object
        filetype: 'miriad', 'uvfits', or 'uvh5'. Ignored if input_data is UVData/HERAData objects

    Returns:
        hd: HERAData object. Will not have data loaded if initialized from string(s).
    '''

    if filetype not in ['miriad', 'uvfits', 'uvh5']:
        raise NotImplementedError("Data filetype must be 'miriad', 'uvfits', or 'uvh5'.")
    if isinstance(input_data, str):  # single visibility data path
        return HERAData(input_data, filetype=filetype)
    elif isinstance(input_data, (UVData, HERAData)):  # single UVData object
        hd = input_data
        hd.__class__ = HERAData
        hd._determine_blt_slicing()
        hd._determine_pol_indexing()
        return hd
    elif isinstance(input_data, collections.Iterable):  # List loading
        if np.all([isinstance(i, str) for i in input_data]):  # List of visibility data paths
            return HERAData(input_data, filetype=filetype)
        elif np.all([isinstance(i, (UVData, HERAData)) for i in input_data]):  # List of uvdata objects
            hd = reduce(operator.add, input_data)
            hd.__class__ = HERAData
            hd._determine_blt_slicing()
            hd._determine_pol_indexing()
            return hd
        else:
            raise TypeError('If input is a list, it must be only strings or only UVData/HERAData objects.')
    else:
        raise TypeError('Input must be a UVData/HERAData object, a string, or a list of either.')


def load_vis(input_data, return_meta=False, filetype='miriad', pop_autos=False, pick_data_ants=True, nested_dict=False):
    '''Load miriad or uvfits files or UVData/HERAData objects into DataContainers, optionally returning
    the most useful metadata. More than one spectral window is not supported. Assumes every baseline
    has the same times present and that the times are in order.

    Arguments:
        input_data: data file path, or UVData/HERAData instance, or list of either strings of data
            file paths or list of UVData/HERAData instances to concatenate into a single dictionary
        return_meta:  boolean, if True: also return antpos, ants, freqs, times, lsts, and pols
        filetype: 'miriad', 'uvfits', or 'uvh5'. Ignored if input_data is UVData/HERAData objects
        pop_autos: boolean, if True: remove autocorrelations
        pick_data_ants: boolean, if True and return_meta=True, return only antennas in data
        nested_dict: boolean, if True replace DataContainers with the legacy nested dictionary filetype
            where visibilities and flags are accessed as data[(0,1)]['xx']

    Returns:
        if return_meta is True:
            (data, flags, antpos, ants, freqs, times, lsts, pols)
        else:
            (data, flags)

        data: DataContainer containing baseline-pol complex visibility data with keys
            like (0,1,'xx') and with shape=(Ntimes,Nfreqs)
        flags: DataContainer containing data flags
        antpos: dictionary containing antennas numbers as keys and position vectors
        ants: ndarray containing unique antenna indices
        freqs: ndarray containing frequency channels (Hz)
        times: ndarray containing julian date bins of data
        lsts: ndarray containing LST bins of data (radians)
        pol: ndarray containing list of polarization strings
    '''

    hd = to_HERAData(input_data, filetype=filetype)
    if hd.data_array is not None:
        d, f, n = hd.build_datacontainers()
    else:
        d, f, n = hd.read()

    # remove autos if requested
    if pop_autos:
        for k in d.keys():
            if k[0] == k[1]:
                del d[k], f[k], n[k]

    # convert into nested dict if necessary
    if nested_dict:
        data, flags = odict(), odict()
        antpairs = [key[0:2] for key in d.keys()]
        for ap in antpairs:
            data[ap] = d[ap]
            flags[ap] = f[ap]
    else:
        data, flags = d, f

    # get meta
    if return_meta:
        antpos, ants = hd.get_ENU_antpos(center=True, pick_data_ants=pick_data_ants)
        antpos = odict(zip(ants, antpos))
        return data, flags, antpos, ants, d.freqs, d.times, d.lsts, d.pols()
    else:
        return data, flags


def write_vis(fname, data, lst_array, freq_array, antpos, time_array=None, flags=None, nsamples=None,
              filetype='miriad', write_file=True, outdir="./", overwrite=False, verbose=True, history=" ",
              return_uvd=False, longitude=21.42830, start_jd=None, instrument="HERA",
              telescope_name="HERA", object_name='EOR', vis_units='uncalib', dec=-30.72152,
              telescope_location=np.array([5109325.85521063, 2005235.09142983, -3239928.42475395]),
              integration_time=None, **kwargs):
    """
    Take DataContainer dictionary, export to UVData object and write to file. See pyuvdata.UVdata
    documentation for more info on these attributes.

    Parameters:
    -----------
    fname : type=str, output filename of visibliity data

    data : type=DataContainer, holds complex visibility data.

    lst_array : type=float ndarray, contains unique LST time bins [radians] of data (center of integration).

    freq_array : type=ndarray, contains frequency bins of data [Hz].

    antpos : type=dictionary, antenna position dictionary. keys are antenna integers and values
             are position vectors in meters in ENU (TOPO) frame.

    time_array : type=ndarray, contains unique Julian Date time bins of data (center of integration).

    flags : type=DataContainer, holds data flags, matching data in shape.

    nsamples : type=DataContainer, holds number of points averaged into each bin in data (if applicable).

    filetype : type=str, filetype to write-out, options=['miriad'].

    write_file : type=boolean, write UVData to file if True.

    outdir : type=str, output directory for output file.

    overwrite : type=boolean, if True, overwrite output files.

    verbose : type=boolean, if True, report feedback to stdout.

    history : type=str, history string for UVData object

    return_uvd : type=boolean, if True return UVData instance.

    longitude : type=float, longitude of observer in degrees East

    start_jd : type=float, starting integer Julian Date of time_array if time_array is None.

    instrument : type=str, instrument name.

    telescope_name : type=str, telescope name.

    object_name : type=str, observing object name.

    vis_unit : type=str, visibility units.

    dec : type=float, declination of observer in degrees North.

    telescope_location : type=ndarray, telescope location in xyz in ITRF (earth-centered frame).

    integration_time : type=float, integration duration in seconds for data_array. This does not necessarily have
        to be equal to the diff(time_array): for the case of LST-binning, this is not the duration of the LST-bin
        but the integration time of the pre-binned data.

    kwargs : type=dictionary, additional parameters to set in UVData object.

    Output:
    -------
    if return_uvd: return UVData instance
    """
    # configure UVData parameters
    # get pols
    pols = np.unique(map(lambda k: k[-1], data.keys()))
    Npols = len(pols)
    polarization_array = np.array(map(lambda p: polstr2num(p), pols))

    # get times
    if time_array is None:
        if start_jd is None:
            raise AttributeError("if time_array is not fed, start_jd must be fed")
        time_array = hc.utils.LST2JD(lst_array, start_jd, longitude=longitude)
    Ntimes = len(time_array)
    if integration_time is None:
        integration_time = np.median(np.diff(time_array)) * 24 * 3600.

    # get freqs
    Nfreqs = len(freq_array)
    channel_width = np.median(np.diff(freq_array))
    freq_array = freq_array.reshape(1, -1)
    spw_array = np.array([0])
    Nspws = 1

    # get baselines keys
    antpairs = sorted(data.antpairs())
    Nbls = len(antpairs)
    Nblts = Nbls * Ntimes

    # reconfigure time_array and lst_array
    time_array = np.repeat(time_array[np.newaxis], Nbls, axis=0).ravel()
    lst_array = np.repeat(lst_array[np.newaxis], Nbls, axis=0).ravel()

    # get data array
    data_array = np.moveaxis(map(lambda p: map(lambda ap: data[str(p)][ap], antpairs), pols), 0, -1)

    # resort time and baseline axes
    data_array = data_array.reshape(Nblts, 1, Nfreqs, Npols)
    if nsamples is None:
        nsample_array = np.ones_like(data_array, np.float)
    else:
        nsample_array = np.moveaxis(map(lambda p: map(lambda ap: nsamples[str(p)][ap], antpairs), pols), 0, -1)
        nsample_array = nsample_array.reshape(Nblts, 1, Nfreqs, Npols)

    # flags
    if flags is None:
        flag_array = np.zeros_like(data_array, np.float).astype(np.bool)
    else:
        flag_array = np.moveaxis(map(lambda p: map(lambda ap: flags[str(p)][ap].astype(np.bool), antpairs), pols), 0, -1)
        flag_array = flag_array.reshape(Nblts, 1, Nfreqs, Npols)

    # configure baselines
    antpairs = np.repeat(np.array(antpairs), Ntimes, axis=0)

    # get ant_1_array, ant_2_array
    ant_1_array = antpairs[:, 0]
    ant_2_array = antpairs[:, 1]

    # get baseline array
    baseline_array = 2048 * (ant_1_array + 1) + (ant_2_array + 1) + 2**16

    # get antennas in data
    data_ants = np.unique(np.concatenate([ant_1_array, ant_2_array]))
    Nants_data = len(data_ants)

    # get telescope ants
    antenna_numbers = np.unique(antpos.keys())
    Nants_telescope = len(antenna_numbers)
    antenna_names = map(lambda a: "HH{}".format(a), antenna_numbers)

    # set uvw assuming drift phase i.e. phase center is zenith
    uvw_array = np.array([antpos[k[1]] - antpos[k[0]] for k in zip(ant_1_array, ant_2_array)])

    # get antenna positions in ITRF frame
    tel_lat_lon_alt = uvutils.LatLonAlt_from_XYZ(telescope_location)
    antenna_positions = np.array(map(lambda k: antpos[k], antenna_numbers))
    antenna_positions = uvutils.ECEF_from_ENU(antenna_positions.T, *tel_lat_lon_alt).T - telescope_location

    # get zenith location: can only write drift phase
    phase_type = 'drift'

    # instantiate object
    uvd = UVData()

    # assign parameters
    params = ['Nants_data', 'Nants_telescope', 'Nbls', 'Nblts', 'Nfreqs', 'Npols', 'Nspws', 'Ntimes',
              'ant_1_array', 'ant_2_array', 'antenna_names', 'antenna_numbers', 'baseline_array',
              'channel_width', 'data_array', 'flag_array', 'freq_array', 'history', 'instrument',
              'integration_time', 'lst_array', 'nsample_array', 'object_name', 'phase_type',
              'polarization_array', 'spw_array', 'telescope_location', 'telescope_name', 'time_array',
              'uvw_array', 'vis_units', 'antenna_positions']
    local_params = locals()

    # overwrite paramters by kwargs
    local_params.update(kwargs)

    # set parameters in uvd
    for p in params:
        uvd.__setattr__(p, local_params[p])

    # write to file
    if write_file:
        if filetype == 'miriad':
            # check output
            fname = os.path.join(outdir, fname)
            if os.path.exists(fname) and overwrite is False:
                if verbose:
                    print("{} exists, not overwriting".format(fname))
            else:
                if verbose:
                    print("saving {}".format(fname))
                uvd.write_miriad(fname, clobber=True)

        else:
            raise AttributeError("didn't recognize filetype: {}".format(filetype))

    if return_uvd:
        return uvd


def update_uvdata(uvd, data=None, flags=None, add_to_history='', **kwargs):
    '''Updates a UVData/HERAData object with data or parameters. Cannot modify the shape of
    data arrays. More than one spectral window is not supported. Assumes every baseline
    has the same times present and that the times are in order.

    Arguments:
        uv: UVData/HERAData object to be updated
        data: dictionary or DataContainer of complex visibility data to update. Keys
            like (0,1,'xx') and shape=(Ntimes,Nfreqs). Default (None) does not update.
        flags: dictionary or DataContainer of data flags to update.
            Default (None) does not update.
        add_to_history: appends a string to the history of the UVData/HERAData object
        kwargs: dictionary mapping updated attributs to their new values.
            See pyuvdata.UVData documentation for more info.
    '''

    # perform update
    original_class = uvd.__class__
    uvd = to_HERAData(uvd)
    uvd.update(data=data, flags=flags)
    uvd.__class__ = original_class

    # set additional attributes
    uvd.history += add_to_history
    for attribute, value in kwargs.items():
        uvd.__setattr__(attribute, value)
    uvd.check()


def update_vis(infilename, outfilename, filetype_in='miriad', filetype_out='miriad',
               data=None, flags=None, add_to_history='', clobber=False, **kwargs):
    '''Loads an existing file with pyuvdata, modifies some subset of of its parameters, and
    then writes a new file to disk. Cannot modify the shape of data arrays. More than one
    spectral window is not supported. Assumes every baseline has the same times present
    and that the times are in order.

    Arguments:
        infilename: filename of the base visibility file to be updated, or UVData/HERAData object
        outfilename: filename of the new visibility file
        filetype_in: either 'miriad' or 'uvfits' (ignored if infile is a UVData/HERAData object)
        filetype_out: either 'miriad' or 'uvfits'
        data: dictionary or DataContainer of complex visibility data to update. Keys
            like (0,1,'xx') and shape=(Ntimes,Nfreqs). Default (None) does not update.
        flags: dictionary or DataContainer of data flags to update.
            Default (None) does not update.
        add_to_history: appends a string to the history of the output file
        clobber: if True, overwrites existing file at outfilename. Always True for uvfits.
        kwargs: dictionary mapping updated attributs to their new values.
            See pyuvdata.UVData documentation for more info.
    '''

    # Load infile
    if isinstance(infilename, (UVData, HERAData)):
        hd = copy.deepcopy(infilename)
    else:
        hd = HERAData(infilename, filetype=filetype_in)
        hd.read()
    update_uvdata(hd, data=data, flags=flags, add_to_history=add_to_history, **kwargs)

    # write out results
    if filetype_out == 'miriad':
        hd.write_miriad(outfilename, clobber=clobber)
    elif filetype_out == 'uvfits':
        hd.write_uvfits(outfilename, force_phase=True, spoof_nonessential=True)
    elif filetype_out == 'uvh5':
        hd.write_uvh5(outfilename, clobber=clobber)
    else:
        raise TypeError("Input filetype must be either 'miriad', 'uvfits', or 'uvh5'.")


def to_HERACal(input_cal):
    '''Converts a string path, UVCal, or HERACal object, or a list of any one of those, to a
    single HERACal object without loading any new calibration solutions.

    Arguments:
        input_cal: path to calfits file, UVCal/HERACal object, or a list of either to combine
            into a single HERACal object

    Returns:
        hc: HERACal object. Will not have calibration loaded if initialized from string(s).
    '''
    if isinstance(input_cal, str):  # single calfits path
        return HERACal(input_cal)
    elif isinstance(input_cal, (UVCal, HERACal)):  # single UVCal/HERACal object
        input_cal.__class__ = HERACal
        return input_cal
    elif isinstance(input_cal, collections.Iterable):  # List loading
        if np.all([isinstance(ic, str) for ic in input_cal]):  # List of calfits paths
            return HERACal(input_cal)
        elif np.all([isinstance(ic, (UVCal, HERACal)) for ic in input_cal]):  # List of UVCal/HERACal objects
            hc = reduce(operator.add, input_cal)
            hc.__class__ = HERACal
            return hc
        else:
            raise TypeError('If input is a list, it must be only strings or only UVCal/HERACal objects.')
    else:
        raise TypeError('Input must be a UVCal/HERACal object, a string, or a list of either.')


def load_cal(input_cal, return_meta=False):
    '''Load calfits files or UVCal/HERACal objects into dictionaries, optionally
    returning the most useful metadata. More than one spectral window is not supported.

    Arguments:
        input_cal: path to calfits file, UVCal/HERACal object, or a list of either
        return_meta: if True, returns additional information (see below)

    Returns:
        if return_meta is True:
            (gains, flags, quals, total_qual, ants, freqs, times, pols)
        else:
            (gains, flags)

        gains: Dictionary of complex calibration gains as a function of time
            and frequency with keys in the (1,'x') format
        flags: Dictionary of flags in the same format as the gains
        quals: Dictionary of of qualities of calibration solutions in the same
            format as the gains (e.g. omnical chi^2 per antenna)
        total_qual: ndarray of total calibration quality for the whole array
            (e.g. omnical overall chi^2)
        ants: ndarray containing unique antenna indices
        freqs: ndarray containing frequency channels (Hz)
        times: ndarray containing julian date bins of data
        pols: list of antenna polarization strings
    '''
    # load HERACal object and extract gains, data, etc.
    hc = to_HERACal(input_cal)
    if hc.gain_array is not None:
        gains, flags, quals, total_qual = hc.build_calcontainers()
    else:
        gains, flags, quals, total_qual = hc.read()

    # return quantities
    if return_meta:
        return gains, flags, quals, total_qual, np.array([ant[0] for ant in hc.ants]), hc.freqs, hc.times, hc.pols
    else:
        return gains, flags


def write_cal(fname, gains, freqs, times, flags=None, quality=None, total_qual=None, write_file=True,
              return_uvc=True, outdir='./', overwrite=False, gain_convention='divide',
              history=' ', x_orientation="east", telescope_name='HERA', cal_style='redundant',
              **kwargs):
    '''Format gain solution dictionary into pyuvdata.UVCal and write to file

    Arguments:
        fname : type=str, output file basename
        gains : type=dictionary, holds complex gain solutions. keys are antenna + pol
                tuple pairs, e.g. (2, 'x'), and keys are 2D complex ndarrays with time
                along [0] axis and freq along [1] axis.
        freqs : type=ndarray, holds unique frequencies channels in Hz
        times : type=ndarray, holds unique times of integration centers in Julian Date
        flags : type=dictionary, holds boolean flags (True if flagged) for gains.
                Must match shape of gains.
        quality : type=dictionary, holds "quality" of calibration solution. Must match
                  shape of gains. See pyuvdata.UVCal doc for more details.
        total_qual : type=dictionary, holds total_quality_array. Key(s) are polarization
            string(s) and values are 2D (Ntimes, Nfreqs) ndarrays.
        write_file : type=bool, if True, write UVCal to calfits file
        return_uvc : type=bool, if True, return UVCal object
        outdir : type=str, output file directory
        overwrite : type=bool, if True overwrite output files
        gain_convention : type=str, gain solutions formatted such that they 'multiply' into data
                          to get model, or 'divide' into data to get model
                          options=['multiply', 'divide']
        history : type=str, history string for UVCal object.
        x_orientation : type=str, orientation of X dipole, options=['east', 'north']
        telescope_name : type=str, name of telescope
        cal_style : type=str, style of calibration solutions, options=['redundant', 'sky']. If
                    cal_style == sky, additional params are required. See pyuvdata.UVCal doc.
        kwargs : additional atrributes to set in pyuvdata.UVCal
    Returns:
        if return_uvc: returns UVCal object
        else: returns None
    '''

    # get antenna info
    ant_array = np.array(sorted(map(lambda k: k[0], gains.keys())), np.int)
    antenna_numbers = copy.copy(ant_array)
    antenna_names = np.array(map(lambda a: "ant{}".format(a), antenna_numbers))
    Nants_data = len(ant_array)
    Nants_telescope = len(antenna_numbers)

    # get polarization info
    pol_array = np.array(sorted(set(map(lambda k: k[1], gains.keys()))))
    jones_array = np.array(map(lambda p: jstr2num(p), pol_array), np.int)
    Njones = len(jones_array)

    # get time info
    time_array = np.array(times, np.float)
    Ntimes = len(time_array)
    time_range = np.array([time_array.min(), time_array.max()], np.float)
    if len(time_array) > 1:
        integration_time = np.median(np.diff(time_array)) * 24. * 3600.
    else:
        integration_time = 0.0

    # get frequency info
    freq_array = np.array(freqs, np.float)
    Nfreqs = len(freq_array)
    Nspws = 1
    freq_array = freq_array[None, :]
    spw_array = np.arange(Nspws)
    channel_width = np.median(np.diff(freq_array))

    # form gain, flags and qualities
    gain_array = np.empty((Nants_data, Nspws, Nfreqs, Ntimes, Njones), np.complex)
    flag_array = np.empty((Nants_data, Nspws, Nfreqs, Ntimes, Njones), np.bool)
    quality_array = np.empty((Nants_data, Nspws, Nfreqs, Ntimes, Njones), np.float)
    total_quality_array = np.empty((Nspws, Nfreqs, Ntimes, Njones), np.float)
    for i, p in enumerate(pol_array):
        if total_qual is not None:
            total_quality_array[0, :, :, i] = total_qual[p].T[None, :, :]
        for j, a in enumerate(ant_array):
            # ensure (a, p) is in gains
            if (a, p) in gains:
                gain_array[j, :, :, :, i] = gains[(a, p)].T[None, :, :]
                if flags is not None:
                    flag_array[j, :, :, :, i] = flags[(a, p)].T[None, :, :]
                else:
                    flag_array[j, :, :, :, i] = np.zeros((Nspws, Nfreqs, Ntimes), np.bool)
                if quality is not None:
                    quality_array[j, :, :, :, i] = quality[(a, p)].T[None, :, :]
                else:
                    quality_array[j, :, :, :, i] = np.ones((Nspws, Nfreqs, Ntimes), np.float)
            else:
                gain_array[j, :, :, :, i] = np.ones((Nspws, Nfreqs, Ntimes), np.complex)
                flag_array[j, :, :, :, i] = np.ones((Nspws, Nfreqs, Ntimes), np.bool)
                quality_array[j, :, :, :, i] = np.ones((Nspws, Nfreqs, Ntimes), np.float)

    if total_qual is None:
        total_quality_array = None

    # Check gain_array for values close to zero, if so, set to 1
    zero_check = np.isclose(gain_array, 0, rtol=1e-10, atol=1e-10)
    gain_array[zero_check] = 1.0 + 0j
    flag_array[zero_check] += True
    if zero_check.max() is True:
        print("Some of values in self.gain_array were zero and are flagged and set to 1.")

    # instantiate UVCal
    uvc = UVCal()

    # enforce 'gain' cal_type
    uvc.cal_type = "gain"

    # create parameter list
    params = ["Nants_data", "Nants_telescope", "Nfreqs", "Ntimes", "Nspws", "Njones",
              "ant_array", "antenna_numbers", "antenna_names", "cal_style", "history",
              "channel_width", "flag_array", "gain_array", "quality_array", "jones_array",
              "time_array", "spw_array", "freq_array", "history", "integration_time",
              "time_range", "x_orientation", "telescope_name", "gain_convention", "total_quality_array"]

    # create local parameter dict
    local_params = locals()

    # overwrite with kwarg parameters
    local_params.update(kwargs)

    # set parameters
    for p in params:
        uvc.__setattr__(p, local_params[p])

    # run check
    uvc.check()

    # write to file
    if write_file:
        # check output
        fname = os.path.join(outdir, fname)
        if os.path.exists(fname) and overwrite is False:
            print("{} exists, not overwriting...".format(fname))
        else:
            print "saving {}".format(fname)
            uvc.write_calfits(fname, clobber=True)

    # return object
    if return_uvc:
        return uvc


def update_uvcal(cal, gains=None, flags=None, quals=None, add_to_history='', **kwargs):
    '''LEGACY CODE TO BE DEPRECATED!
    Update UVCal object with gains, flags, quals, history, and/or other parameters
    Cannot modify the shape of gain arrays. More than one spectral window is not supported.

    Arguments:
        cal: UVCal/HERACal object to be updated
        gains: Dictionary of complex calibration gains with shape=(Ntimes,Nfreqs)
            with keys in the (1,'x') format. Default (None) leaves unchanged.
        flags: Dictionary like gains but of flags. Default (None) leaves unchanged.
        quals: Dictionary like gains but of per-antenna quality. Default (None) leaves unchanged.
        add_to_history: appends a string to the history of the output file
        overwrite: if True, overwrites existing file at outfilename
        kwargs: dictionary mapping updated attributs to their new values.
            See pyuvdata.UVCal documentation for more info.
    '''
    original_class = cal.__class__
    cal.__class__ = HERACal
    cal._extract_metadata()
    cal.update(gains=gains, flags=flags, quals=quals)

    # Check gain_array for values close to zero, if so, set to 1
    zero_check = np.isclose(cal.gain_array, 0, rtol=1e-10, atol=1e-10)
    cal.gain_array[zero_check] = 1.0 + 0j
    cal.flag_array[zero_check] += True
    if zero_check.max() is True:
        print("Some of values in self.gain_array were zero and are flagged and set to 1.")

    # Set additional attributes
    cal.history += add_to_history
    for attribute, value in kwargs.items():
        cal.__setattr__(attribute, value)
    cal.check()
    cal.__class__ = original_class


def update_cal(infilename, outfilename, gains=None, flags=None, quals=None, add_to_history='', clobber=False, **kwargs):
    '''Loads an existing calfits file with pyuvdata, modifies some subset of of its parameters,
    and then writes a new calfits file to disk. Cannot modify the shape of gain arrays.
    More than one spectral window is not supported.

    Arguments:
        infilename: filename of the base calfits file to be updated, or UVCal object
        outfilename: filename of the new calfits file
        gains: Dictionary of complex calibration gains with shape=(Ntimes,Nfreqs)
            with keys in the (1,'x') format. Default (None) leaves unchanged.
        flags: Dictionary like gains but of flags. Default (None) leaves unchanged.
        quals: Dictionary like gains but of per-antenna quality. Default (None) leaves unchanged.
        add_to_history: appends a string to the history of the output file
        clobber: if True, overwrites existing file at outfilename
        kwargs: dictionary mapping updated attributs to their new values.
            See pyuvdata.UVCal documentation for more info.
    '''
    # Load infile
    if isinstance(infilename, (UVCal, HERACal)):
        cal = copy.deepcopy(infilename)
    else:
        cal = HERACal(infilename)
        cal.read()

    update_uvcal(cal, gains=gains, flags=flags, quals=quals, add_to_history=add_to_history, **kwargs)

    # Write to calfits file
    cal.write_calfits(outfilename, clobber=clobber)


def load_npz_flags(npzfile):
    '''Load flags from a npz file (like those produced by hera_qm.xrfi) and converts
    them into a DataContainer. More than one spectral window is not supported. Assumes
    every baseline has the same times present and that the times are in order.

    Arguments:
        npzfile: path to .npz file containing flags and array metadata
    Returns:
        flags: Dictionary of boolean flags as a function of time and
            frequency with keys in the (1,'x') format
    '''
    npz = np.load(npzfile)
    pols = [polnum2str(p) for p in npz['polarization_array']]
    nTimes = len(np.unique(npz['time_array']))
    nAntpairs = len(npz['antpairs'])
    nFreqs = npz['flag_array'].shape[2]
    assert npz['flag_array'].shape[0] == nAntpairs * nTimes, \
        'flag_array must have flags for all baselines for all times.'

    flags = {}
    for p, pol in enumerate(pols):
        flag_array = np.reshape(npz['flag_array'][:, 0, :, p], (nTimes, nAntpairs, nFreqs))
        for n, (i, j) in enumerate(npz['antpairs']):
            flags[i, j, pol] = flag_array[:, n, :]
    return DataContainer(flags)
