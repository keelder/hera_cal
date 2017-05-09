'''
Tools for dealing with redundant array configurations.
'''

import numpy as np
import aipy as a
import pylab as p
import time 
import omnical
import multiprocessing as mpr
import scipy.sparse as sps

def fit_line(phs, fqs, valid, offset=False):
    fqs = fqs.compress(valid)
    dly = phs.compress(valid)
    B = np.zeros((fqs.size,1)); B[:,0] = dly
    A = np.zeros((fqs.size,1)); A[:,0] = fqs*2*np.pi#; A[:,1] = 1
    dt = np.linalg.lstsq(A,B)[0][0][0]
    return dt

def redundant_bl_cal_simple(d1,w1,d2,w2,fqs, window='none', finetune=True, verbose=False, average=False):
    '''Gets the phase differnce between two baselines by using the fourier transform and a linear fit to that residual slop. 
        Parameters
        ----------
        d1,d2 : NXM numpy arrays.
            Data arrays to find phase difference between. First axis is time, second axis is frequency. 
        w1,w2 : NXM numpy arrays. 
            corrsponding data weight arrays.
        fqs   : 1XM numpy array
            Array of frequencies in GHz.
        window: str
            Name of window function to use in fourier transform. Default is 'none'.
        finetune  : boolean 
            Flag if you want to fine tune the phase fit with a linear fit. Default is true.
        verbose: boolean
            Be verobse. Default is False.
        plot: boolean
            Turn on low level plotting of phase ratios. Default is False.
        average: boolean
            Average the data in time before applying analysis. collapses NXM -> 1XM.

        Returns
        -------
        delays : ndarrays
            Array of delays (if average == False), or single delay.    
    
'''
    d12 = d2 * np.conj(d1)
    # For 2D arrays, assume first axis is time. 
    if average:
        if d12.ndim > 1:
            d12_sum = np.sum(d12,axis=0).reshape(1,-1)
            d12_wgt = np.sum(w1*w1,axis=0).reshape(1,-1)
        else:
            d12_sum = d12.reshape(1,-1)
            d12_wgt = w1.reshape(1,-1)*w2.reshape(1,-1)
    else:
        d12_sum = d12
        d12_wgt = w1*w2
    #normalize data to maximum so that we minimize fft articats from RFI
    d12_sum *= d12_wgt
    d12_sum = d12_sum/np.where(np.abs(d12_sum)==0., 1., np.abs(d12_sum)) 
    window = a.dsp.gen_window(d12_sum[0,:].size, window=window)
    dlys = np.fft.fftfreq(fqs.size, fqs[1]-fqs[0])
    # FFT and deconvolve the weights to get the phs
    _phs = np.fft.fft(window*d12_sum,axis=-1)
    _wgt = np.fft.fft(window*d12_wgt,axis=-1)
    _phss = _phs
    _phss = np.abs(_phss)
    #get bin of phase
    mxs = np.argmax(_phss, axis=-1)
    #Fine tune with linear fit.
    mxs[mxs>_phss.shape[-1]/2] -= _phss.shape[-1]
    dtau = mxs / (fqs[-1] - fqs[0])
    mxs = np.dot(mxs.reshape(len(mxs),1),np.ones((1,3),dtype=int)) + np.array([-1,0,1])
    taus = np.sum(_phss[np.arange(mxs.shape[0],dtype=int).reshape(-1,1),mxs] * dlys[mxs],axis=-1) / np.sum(_phss[np.arange(mxs.shape[0]).reshape(-1,1),mxs],axis=-1)
    dts = []
    if finetune:
    #loop over the linear fits
        t1 = time.time()
        for ii,(tau,d) in enumerate(zip(taus,d12_sum)):
            valid = np.where(d != 0, 1, 0) # Throw out zeros, which NaN in the log below
            valid = np.logical_and(valid, np.logical_and(fqs>.11,fqs<.19))
            dly = np.angle(d*np.exp(-2j*np.pi*tau*fqs))
            dt = fit_line(dly,fqs,valid)
            dts.append(dt)
#            if plot:
#                p.subplot(411)
#                p.plot(fqs,np.angle(d12_sum[ii]), linewidth=2)
#                p.plot(fqs,d12_sum[ii], linewidth=2)
#                p.plot(fqs, np.exp((2j*np.pi*fqs*(tau+dt))+off))
#                p.hlines(np.pi, .1,.2,linestyles='--',colors='k')
#                p.hlines(-np.pi, .1,.2,linestyles='--',colors='k')
#                p.subplot(412)
#                p.plot(fqs,np.unwrap(dly)+2*np.pi*tau*fqs, linewidth=2)
#                p.plot(fqs,dly+2*np.pi*tau*fqs, linewidth=2,ls='--')
#                p.plot(fqs,2*np.pi*tau*fqs, linewidth=2,ls='-.')
#                p.plot(fqs,2*np.pi*(tau+dt)*fqs + off, linewidth=2,ls=':')
#                p.subplot(413)
#                p.plot(dlys, np.abs(_phs[ii]),'-.')
#                p.xlim(-400,400)
#                p.subplot(414)
#                p.plot(fqs,dly, linewidth=2)
#                p.plot(fqs,off+dt*fqs*2*np.pi, '--')
#                p.hlines(np.pi, .1,.2,linestyles='--',colors='k')
#                p.hlines(-np.pi, .1,.2,linestyles='--',colors='k')
#                print 'tau=', tau
#                print 'tau + dt=', tau+dt
#                p.xlabel('Frequency (GHz)', fontsize='large')
#                p.ylabel('Phase (radians)', fontsize='large')
#        p.show()
        dts = np.array(dts)

    info = {'dtau':dts, 'mx':mxs}
    if verbose: print info, taus, taus+dts
    return taus+dts


class FirstCalRedundantInfo(omnical.info.RedundantInfo):
    '''
        The extra meta data added to the RedundantInfo object from omnical are:
        self.nant : number of antennas
        self.A:  coefficient matrix for firstcal delay calibration. (Nmeasurements, Nants).
                 Measurements are ratios of redundant baselines.
    '''
    def __init__(self, nant):
        omnical.info.RedundantInfo.__init__(self)
        self.nant = nant

    def order_data(self, dd):
        '''Create a data array ordered for use in _omnical.redcal.  'dd' is
        a dict whose keys are (i,j) antenna tuples; antennas i,j should be ordered to reflect
        the conjugation convention of the provided data.  'dd' values are 2D arrays
        of (time,freq) data.''' # XXX does time/freq ordering matter.  should data be 2D instead?
        return np.array([dd[bl] if dd.has_key(bl) else dd[bl[::-1]].conj()
            for bl in self.bl_order()]).transpose((1,2,0))

    def bl_index(self,bl):
        '''Gets the baseline index from bl_order for a given baseline, bl'''
        try: return self._bl2ind[bl]
        except(AttributeError):
            self._bl2ind = {}
            for x,b in enumerate(self.bl_order()): self._bl2ind[b] = x
            return self._bl2ind[bl]

    def blpair_index(self,blpair):
        try: return self._blpair2ind[blpair]
        except:
            self._blpair2ind = {}
            for x,bp in enumerate(self.bl_pairs): self._blpair2ind[bp] = x
            return self._blpair2ind[blpair]

    def blpair2antind(self,blpair):
        try: return self._blpair2antind[blpair]
        except:
            self._blpair2antind = {}
            for bp in self.bl_pairs: self._blpair2antind[bp] = map(self.ant_index,np.array(bp).flatten())
            return self._blpair2antind[blpair]

    def init_from_reds(self, reds, antpos):
        '''Initialize RedundantInfo from a list where each entry is a group of redundant baselines.
        Each baseline is a (i,j) tuple, where i,j are antenna indices.  To ensure baselines are
        oriented to be redundant, it may be necessary to have i > j.  If this is the case, then
        when calibrating visibilities listed as j,i data will have to be conjugated. After initializing,
        the coefficient matrix for deducing delay solutions per antennas (for firstcal) is created by
        modeling it as per antenna delays.'''
        self.reds = [[(int(i),int(j)) for i,j in gp] for gp in reds]
        self.init_same(self.reds)
        #new stuff for first cal
        self.bl_pairs = [(bl1,bl2) for ublgp in reds for i,bl1 in enumerate(ublgp) for bl2 in ublgp[i+1:]]
        A = np.zeros((len(self.bl_pairs),len(self.subsetant)))
        for n, bp in enumerate(self.bl_pairs):
            i,j,k,l = self.blpair2antind(bp)
            A[n,i] += 1
            A[n,j] += -1
            A[n,k] += -1
            A[n,l] += 1
        self.A = A
        # XXX nothing up to this point requires antloc; in principle, degenM can be deduced
        # from reds alone, removing need for antpos.  So that'd be nice, someday
        self.antloc = antpos.take(self.subsetant, axis=0).astype(np.float32)
        self.ubl = np.array([np.mean([antpos[j]-antpos[i] for i,j in ublgp],axis=0) for ublgp in reds], dtype=np.float32)

    def get_reds(self):
        '''After initialization, return redundancies.'''
        try: return self.reds
        except(AttributeError):
            print 'Initialize info class!'


class FirstCal(object):
    def __init__(self, data, wgts, fqs, info):
        self.data = data
        self.fqs = fqs
        self.info = info
        self.wgts = wgts

    def data_to_delays(self, **kwargs):
        '''data = dictionary of visibilities.
           info = FirstCalRedundantInfo class
           can give it kwargs:
                supports 'window': window function for fourier transform. default is none
                         'tune'  : to fit and remove a linear slope to phase.
                         'plot'  : Low level plotting in the red.redundant_bl_cal_simple script.
                         'clean' : Clean level when deconvolving sampling function out.
           Returns 2 dictionaries:
                1. baseline pair : delays
                2. baseline pari : offset
        '''
        verbose = kwargs.get('verbose', False)
        blpair2delay = {}
        blpair2offset = {}
        dd = self.info.order_data(self.data)
        ww = self.info.order_data(self.wgts)
        for (bl1, bl2) in self.info.bl_pairs:
            if verbose:
                print (bl1, bl2)
            d1 = dd[:, :, self.info.bl_index(bl1)]
            w1 = ww[:, :, self.info.bl_index(bl1)]
            d2 = dd[:, :, self.info.bl_index(bl2)]
            w2 = ww[:, :, self.info.bl_index(bl2)]
            delay = redundant_bl_cal_simple(d1, w1, d2, w2, self.fqs, **kwargs)
            blpair2delay[(bl1, bl2)] = delay
        return blpair2delay

    def get_N(self, nblpairs):
        return sps.eye(nblpairs)

    def get_M(self, **kwargs):
        blpair2delay = self.data_to_delays(**kwargs)
        sz = len(blpair2delay[blpair2delay.keys()[0]])
        M = np.zeros((len(self.info.bl_pairs), sz))
        for pair in blpair2delay:
            M[self.info.blpair_index(pair), :] = blpair2delay[pair]
        return M

    def run(self, **kwargs):
        '''Runs firstcal after the class initialized.
           returns:
                 dict: antenna delay pair with delay in nanoseconds.'''
        verbose = kwargs.get('verbose', False)
        # make measurement matrix
        print "Geting M,O matrix"
        self.M = self.get_M(**kwargs)
        print "Geting N matrix"
        N = self.get_N(len(self.info.bl_pairs))
        # XXX This needs to be addressed. If actually do invers, slows code way down.
        # self._N = np.linalg.inv(N)
        self._N = N  # since just using identity now

        # get coefficients matrix,A
        self.A = sps.csr_matrix(self.info.A)
        print 'Shape of coefficient matrix: ', self.A.shape

#        solve for delays
        print "Inverting A.T*N^{-1}*A matrix"
        invert = self.A.T.dot(self._N.dot(self.A)).todense()  # make it dense for pinv
        dontinvert = self.A.T.dot(self._N.dot(self.M))  # converts it all to a dense matrix
#        definitely want to use pinv here and not solve since invert is probably singular.
        self.xhat = np.dot(np.linalg.pinv(invert), dontinvert)
        # turn solutions into dictionary
        return dict(zip(self.info.subsetant, self.xhat))
