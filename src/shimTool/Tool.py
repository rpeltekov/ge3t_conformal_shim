"""The Shim Tool Object for orchestrating the shim process."""

import os
import pickle
import sys
from datetime import datetime
from typing import List

import numpy as np

from shimTool.dicomUtils import *

# Import the custom client classes and util functions
from shimTool.exsi_client import exsi
from shimTool.guiUtils import Trigger, ellipsoidROI
from shimTool.shim_client import shim
from shimTool.shimCompute import *
from shimTool.utils import *


class Tool:
    def __init__(self, config, debugging=True):

        # ----------- Shim Tool Essential Attributes ----------- #
        self.config = config
        self.debugging = debugging

        self.examDateTime = datetime.now()
        self.examDateString = self.examDateTime.strftime("%Y%m%d_%H%M%S")

        self.scannerLog = os.path.join(self.config["rootDir"], self.config["scannerLog"])
        self.shimLog = os.path.join(self.config["rootDir"], self.config["shimLog"])
        self.guiLog = os.path.join(self.config["rootDir"], self.config["guiLog"])

        self.latestStateSavePath = os.path.join(self.config["rootDir"], "toolStates", "shimToolLatestState.pkl")

        # Create the log files if they don't exist / empty them if they already have things there
        for log in [
            self.scannerLog,
            self.shimLog,
            self.guiLog,
            self.latestStateSavePath,
        ]:
            if not os.path.exists(log):
                # create the directory and file
                os.makedirs(os.path.dirname(log), exist_ok=True)
        for log in [self.scannerLog, self.shimLog, self.guiLog]:
            with open(log, "w"):  # remake the file empty
                print(log)
                pass

        # ----------- Clients ----------- #
        # Start the connection from the Shim client.
        self.shimInstance = shim(self.config, self.shimLog, debugging=self.debugging)

        # Start the connection to the Scanner via ExSI.
        self.exsiInstance = exsi(
            self.config,
            self.shimInstance.shimZero,
            self.shimInstance.shimSetCurrentManual,
            debugging=self.debugging,
            output_file=self.scannerLog,
        )

        # connect the clear queue commands so that they can be called from the other client
        self.shimInstance.clearExsiQueue = self.exsiInstance.clear_command_queue
        self.exsiInstance.clearShimQueue = self.shimInstance.clearCommandQueue

        # ----------- Shim Tool Parameters ----------- #
        self.maxDeltaTE = 3500  # 2000 us = 2 ms
        self.minDeltaTE = 100  # 2000 us = 2 ms
        self.deltaTE = 3500
        self.minGradientCalStrength = 10  # 100 mA
        self.maxGradientCalStrength = 200  # 2 A
        self.gradientCalStrength = 60  # max 300 -- the value at which to record basis map for lin shims
        self.minCalibrationCurrent = 100  # 100 mA
        self.maxCalibrationCurrent = 2000  # 2 A
        self.loopCalCurrent = 1000  # 1 A
        self.shimMode = 0  # 0 for Slice-Wise or 1 for Volume shimming
        self.shimModes = {0: "Slice-Wise", 1: "Volume"}

        # ----------- Shim Tool State ----------- #
        # Scan session attributes
        self.autoPrescanDone = False

        self.assetCalibrationDone = False

        self.roiEditorEnabled = False

        self.gehcExamDataPath = None  # the path to the exam data on the GE Server
        self.localExamRootDir = None  # Where the raw dicom data gets stored, once pulled from the GE Server
        self.resultsDir = None  # location to save figures to
        self.backgroundDCMdir = None  # the specific local Dicom Directory for the background image

        # 3d data arrays
        self.backgroundB0Map: np.ndarray = None  # 3d array of the background b0 map
        self.rawBasisB0maps: List[np.ndarray] = [
            None for _ in range(self.shimInstance.numLoops + 3)
        ]  # 3d arrays of the basis b0 maps with background
        self.basisB0maps: List[np.ndarray] = [
            None for _ in range(self.shimInstance.numLoops + 3)
        ]  # 3d arrays of the basis b0 maps without background
        self.expectedB0Map: np.ndarray = (
            None  # 3d array of the shimmed b0 map; Shimming is Slice-Wise -> i.e. one slice is filled at a time per solution
        )
        self.shimmedB0Map: np.ndarray = (
            None  # 3d array of the shimmed b0 map; Shimming is Slice-Wise -> i.e. one slice is filled at a time
        )

        # solution constants; solutions based on the basis maps, not necessary the actual current / Hz / lingrad values
        self.solutions: List[np.ndarray] = None
        # the actual values that will be used to apply the shim currents/ cf offset / lingrad value
        self.solutionValuesToApply: List[np.ndarray] = None  # will be in form of Hz, number for the lingrad, and Amps

        # state getter functions
        self.obtainedBackground = lambda: self.backgroundB0Map is not None
        self.obtainedBasisMaps = lambda: self.basisB0maps[0] is not None
        self.obtainedSolutions = lambda: self.solutions is not None
        self.obtainedShimmedB0Map = lambda: self.shimmedB0Map is not None

        # this is the base solution that is provided by autoprescan or when user overwrites background with existing solution
        self.principleSols = self.resetPrincipleSols()

        self.ROI = ellipsoidROI()
        # masks
        self.roiMask: np.ndarray = None  # 3d boolean mask in the same shape as the background data
        self.finalMask: np.ndarray = (
            None  # the intersection of roi, and all nonNan sections of background and basis maps
        )

        # the stat string outputs
        self.shimStatStrs: List[List[str]] = [None, None, None]  # string form stats
        self.shimStats: List[List] = [None, None, None]  # numerical form stats

        # ----------- Shim Tool GUI Parameters ----------- #
        # the 3d data for each respective view; they should be cropped with respect to the Final Mask when they are set by the shimTool
        self.viewData = np.array(
            [
                None,  # 3D data, unfilled, for roi view
                # three sets of 3D data, unfilled, for shim view (background, estimated, actual)
                np.array([None, None, None], dtype=object),
                # 4D data, unfilled, for basis views
                np.array([None for _ in range(self.shimInstance.numLoops + 3)], dtype=object),
            ],
            dtype=object,
        )

    # ----------- Shim Tool Data/State Collection Functions ----------- #

    def transferScanData(self):
        self.log(f"Initiating transfer using rsync.")
        if self.exsiInstance.examNumber is None:
            self.log("Error: No exam number found in the exsi client instance.")
            return
        if self.gehcExamDataPath is None:
            self.gehcExamDataPath = setGehcExamDataPath(
                self.exsiInstance.examNumber,
                self.config["host"],
                self.config["hvPort"],
                self.config["hvUser"],
                self.config["hvPassword"],
            )
            self.log(f"obtained exam data path: {self.gehcExamDataPath}")
        execRsyncCommand(
            self.config["hvPassword"],
            self.config["hvUser"],
            self.config["host"],
            self.gehcExamDataPath + "/*",
            self.localExamRootDir,
        )

    def getLatestData(self, stride=1, offset=0):
        latestDCMDir = listSubDirs(self.localExamRootDir)[-1]
        res = extractBasicImageData(latestDCMDir, stride, offset)
        self.viewData[0] = res[0]

    def getROIBackgound(self):
        self.log("extracting the background mag image")
        res = extractBasicImageData(self.backgroundDCMdir, stride=3, offset=0)
        self.log("done extracting the background mag image")
        self.viewData[0] = res[0]

    def saveState(self):
        """Save all the state attributes so that they can be reloaded later.
        Main usecase for this is not having to rescan things so that you can debug the tool faster
        """

        # put all the attributes into a list, and save them to file that you can unpack easy
        attr_names = [
            "assetCalibrationDone",
            "autoPrescanDone",
            "roiEditorEnabled",
            "backgroundB0Map",
            "rawBasisB0maps",
            "basisB0maps",
            "expectedB0Map",
            "viewData",
            "shimmedB0Map",
            "solutions",
            "solutionValuesToApply",
            "principleSols",
            "roiMask",
            "finalMask",
            "shimStatStrs",
            "shimStats",
            "gehcExamDataPath",
            "localExamRootDir",
            "resultsDir",
            "backgroundDCMdir",
        ]

        # Create a dictionary to hold your attributes
        attr_dict = {name: getattr(self, name) for name in attr_names if hasattr(self, name)}

        with open(self.latestStateSavePath, "wb") as f:
            pickle.dump(attr_dict, f)

        self.log("Saved the state of the tool.")

    def loadState(self):
        """Load the state attributes from the save file"""
        """This function is also super useful for printing out debug statements about the state of the app more quickly"""

        if not os.path.exists(self.latestStateSavePath):
            self.log(f"There is nothing saved to load at {self.latestStateSavePath}")
            return

        self.log("Loading the state of the tool.")

        # Check if the file is empty
        if os.path.getsize(self.latestStateSavePath) == 0:
            self.log(f"The file at {self.latestStateSavePath} is empty.")
            return

        with open(self.latestStateSavePath, "rb") as f:
            try:
                attr_dict = pickle.load(f)
                for name, value in attr_dict.items():
                    setattr(self, name, value)
            except EOFError as e:
                self.log(f"ERROR: Failed to load state: {e}")
                return

    # ----------- Shim Tool management and compute Functions ----------- #

    def computeMask(self):
        """compute the mask for the shim images"""
        self.finalMask = createMask(self.backgroundB0Map, self.basisB0maps, self.ROI.getROIMask())
        self.log(f"Computed Mask from background, basis and ROI.")

    def applyMask(self):
        """Update the viewable data with the mask applied"""

        maps = [self.backgroundB0Map, self.expectedB0Map, self.shimmedB0Map]

        # send the masked versions of the data to the GUI
        for i, map in enumerate(maps):
            if map is not None:
                toViewer = np.copy(map)
                toViewer[~self.finalMask] = np.nan
                self.viewData[1][i] = toViewer

        for i, basis in enumerate(self.basisB0maps):
            if basis is not None:
                toViewer = np.copy(basis)
                toViewer[~self.finalMask] = np.nan
                self.viewData[2][i] = toViewer

        self.log(f"Masked obtained data and 'sent to GUI.'")

    def setShimMode(self, mode):
        """
        Set the shim mode for the scanner, either Volume or Slice-Wise.
        This will determine how the shimming is done.
        """
        self.shimMode = mode
        self.resetPrincipleSols()

    def resetPrincipleSols(self):
        self.principleSols = np.array([0.0 for _ in range(4 + self.shimInstance.numLoops)], dtype=np.float64)
        if self.exsiInstance.ogCenterFrequency is not None:
            self.principleSols[0] = self.exsiInstance.ogCenterFrequency
        if self.exsiInstance.ogLinearGradients is not None:
            self.principleSols[1:4] = self.exsiInstance.ogLinearGradients

    def resetShimSols(self):
        self.solutions = None
        self.solutionValuesToApply = None
        self.shimStats = [None, None, None]
        self.shimStatStrs = [None, None, None]
        self.expectedB0Map = None
        self.shimmedB0Map = None

    def overwriteBackground(self, sliceIdx=None):
        """
        Save the current solution and shimmed scan as the new "background."
        This will be used as a new background, and all of the "shimmed" scans done till now will be deleted.
        The solution will also be saved as a "principle solution" as if a prescan was done and it landed at these solved values
        """
        if self.solutionValuesToApply is not None and sliceIdx is not None:
            self.principleSols = self.principleSols + self.solutionValuesToApply[sliceIdx]
        self.backgroundB0Map = self.shimmedB0Map
        self.resetShimSols()
        self.recomputeCurrentsAndView()

    def computeBackgroundB0map(self):
        # assumes that you have just gotten background by queueBasisPairScan
        b0maps = compute_b0maps(1, self.localExamRootDir)
        self.backgroundDCMdir = listSubDirs(self.localExamRootDir)[-1]
        self.backgroundB0Map = b0maps[0]

        self.computeMask()
        self.applyMask()

    def computeBasisB0maps(self):
        # assumes that you have just gotten background by queueBasisPairScan
        self.rawBasisB0maps = compute_b0maps(self.shimInstance.numLoops + 3, self.localExamRootDir)

    def computeShimCurrents(self):
        """
        Compute the optimal solutions (currents and lin gradients and cf) for every slice
        Save the generated expected B0 map to the expectedB0map array
        """
        # run whenever both backgroundB0Map and basisB0maps are computed or if one new one is obtained
        self.basisB0maps = subtractBackground(self.backgroundB0Map, self.rawBasisB0maps)
        self.computeMask()

        self.solutions = [None for _ in range(self.backgroundB0Map.shape[1])]
        for i in range(self.backgroundB0Map.shape[1]):
            # want to include slice in front and behind in the mask when solving currents though:
            mask = maskOneSlice(self.finalMask, i)
            if i > 0:
                mask = np.logical_or(mask, maskOneSlice(self.finalMask, i - 1))
            if i < self.backgroundB0Map.shape[1] - 1:
                mask = np.logical_or(mask, maskOneSlice(self.finalMask, i + 1))
            # NOTE: the first and last current that is solved will be for an empty slice...
            self.solutions[i] = solveCurrents(
                self.backgroundB0Map,
                self.basisB0maps,
                mask,
                self.gradientCalStrength,
                self.loopCalCurrent,
                debug=self.debugging,
            )

        self.getSolutionsToApply()  # compute the actual values we will apply to the shim system from these solutions

        # if not all currents are none
        if not all([c is None for c in self.solutions]):
            self.expectedB0Map = self.backgroundB0Map.copy()
            for i in range(self.backgroundB0Map.shape[1]):
                if self.solutions[i] is not None:
                    self.expectedB0Map[:, i, :] += self.solutions[i][0] * np.ones(self.backgroundB0Map[:, 0, :].shape)
                    numIter = self.shimInstance.numLoops + 3
                    for j in range(numIter):
                        # self.log(f"DEBUG: adding current {j} to shimData[1][{i}]")
                        self.expectedB0Map[:, i, :] += self.solutions[i][j + 1] * self.basisB0maps[j][:, i, :]
                else:
                    self.expectedB0Map[:, i, :] = np.nan
            self.applyMask()
            self.log("Computed solutions and created new estimate shim maps")
            return True
        else:
            self.log("Error: Could not solve for currents. Look at error hopefully in output")
            return False

    def computeShimmedB0Map(self, idx):
        """Compute the just obtained b0map of the shimmed background, for the specific slice selected"""
        b0maps = compute_b0maps(1, self.localExamRootDir)
        if self.shimmedB0Map is None:
            self.shimmedB0Map = np.full_like(b0maps[0], np.nan)
        self.shimmedB0Map[:, idx, :] = b0maps[0][:, idx, :]
        self.applyMask()

    def evaluateShimImages(self):
        """evaluate the shim images (with the final mask applied) and store the stats in the stats array."""
        for i, map in enumerate([self.backgroundB0Map, self.expectedB0Map, self.shimmedB0Map]):
            if map is not None:
                self.shimStatStrs[i] = [None for _ in range(self.backgroundB0Map.shape[1])]
                self.shimStats[i] = [None for _ in range(self.backgroundB0Map.shape[1])]
                for j in range(self.backgroundB0Map.shape[1]):
                    mask = maskOneSlice(self.finalMask, j)
                    if not np.isnan(map[mask]).all():
                        statsstr, stats = evaluate(map[mask], self.debugging)
                        self.shimStatStrs[i][j] = statsstr
                        self.shimStats[i][j] = stats

    def evaluateAppliedShims(self, sliceIdx):
        """
        Compare the expected vs actual performance of every shim loop / linear gradient / CF offset and save the difference.
        Helpful to evaluate if the solutions are actually what is being applied.
        """
        b0maps = compute_b0maps(self.shimInstance.numLoops + 4, self.localExamRootDir)
        for i in range(len(b0maps)):
            # save the b0map to the eval folder
            evalDir = os.path.join(
                self.config["rootDir"],
                "results",
                self.exsiInstance.examNumber,
                "eval",
                f"slice_{sliceIdx}",
            )
            if not os.path.exists(evalDir):
                os.makedirs(evalDir)
            np.save(os.path.join(evalDir, f"b0map{i}.npy"), b0maps[i][:, sliceIdx, :])
            # compute the difference from the expected b0map
            expected = np.copy(self.backgroundB0Map[:, sliceIdx, :])
            if i == 0:
                expected += self.solutions[sliceIdx][i] * np.ones(expected.shape)
            else:
                expected += self.solutions[sliceIdx][i] * self.basisB0maps[i - 1][:, sliceIdx, :]

            np.save(os.path.join(evalDir, f"expected{i}.npy"), expected)

            difference = b0maps[i][:, sliceIdx, :] - expected
            fig, ax = plt.subplots(figsize=(8, 6))
            im = ax.imshow(difference, cmap="jet", vmin=-100, vmax=100)
            cbar = plt.colorbar(im)

            plt.title(f"difference basis{i}, slice{sliceIdx}", size=10)
            plt.axis("off")

            fig.savefig(
                os.path.join(evalDir, f"difference{i}.png"),
                bbox_inches="tight",
                transparent=False,
            )
            plt.close(fig)

    def getSolutionsToApply(self):
        """From the Solutions, set the actual values that will be applied to the shim system."""
        # cf does not change.
        self.solutionValuesToApply = [
            np.copy(self.solutions[i]) if self.solutions[i] is not None else None for i in range(len(self.solutions))
        ]
        for i in range(len(self.solutions)):
            if self.solutions[i] is not None:
                for j in range(1, 4):
                    # update the lingrad values
                    self.solutionValuesToApply[i][j] = self.solutionValuesToApply[i][j] * self.gradientCalStrength
                for j in range(4, len(self.solutions[i])):
                    # update the current values
                    self.solutionValuesToApply[i][j] = self.solutionValuesToApply[i][j] * self.loopCalCurrent

    # ----------- SHIM Sub Operations and macro function helpers ----------- #

    def setCenterFrequency(self, deltaCF=0):
        """Offset the center frequency of the scanner by deltaCF"""
        newCF = int(round(self.principleSols[0] + deltaCF))
        self.log(f"DEBUG: Setting center frequency from {self.principleSols[0]} to {newCF}")
        self.exsiInstance.sendSetCenterFrequency(newCF)

    def setLinGradients(self, linGrad=[0.0, 0.0, 0.0]):
        """Set the new gradient as offset from the prescan set ones"""
        # if lingrad is a list of 3 ints, make them numpy array
        if type(linGrad) == list:
            linGrad = np.array(linGrad, dtype=np.float64)

        linGrad += self.principleSols[1:4]

        linGrad = np.round(linGrad).astype(int)
        self.exsiInstance.sendSetShimValues(*linGrad)

    def sendSyncedLoopSolution(self, channel: int, sliceIdx: int = None, ZeroOthers=False, calibration=False):
        """Send a shim loop set current command, but via the ExSI client
        to ensure that the commands are synced with other exsi commands."""
        principleOffset = self.principleSols[channel]
        solutionToApply = self.solutionValuesToApply[sliceIdx][channel + 4]
        if calibration:
            solutionToApply = self.loopCalCurrent
        current += solutionToApply + principleOffset
        solution = self.solutions[sliceIdx][channel + 4]
        self.log(
            f"Queueing loop {channel} to {current:.3f}: solution={solution:.3f}, solToAply={solutionToApply:.3f}, principle={principleOffset:.3f}"
        )
        self.exsiInstance.send(f"X {channel} {current}")

        if ZeroOthers:
            # if this is some calibration scan, we want to zero all the other loops (or set them to their principle values)
            for i in range(self.shimInstance.numLoops):
                if i != channel:
                    self.sendSyncedLoopSolution(i, sliceIdx, ZeroOthers=False, calibration=calibration)

    def queueTwoFgreSequences(self):
        """
        once the b0map sequence is loaded, subroutines are iterated along with cvs to obtain basis maps.
        linGrad should be a list of 3 floats if it is not None
        """
        cvs = {"act_tr": 6000, "act_te": 1104, "rhrcctrl": 13, "rhimsize": 64}
        for i in range(2):
            self.exsiInstance.sendSelTask()
            self.exsiInstance.sendActTask()
            for cv in cvs.keys():
                if cv == "act_te":
                    if i == 0:
                        self.exsiInstance.sendSetCV(cv, cvs[cv])
                    else:
                        self.exsiInstance.sendSetCV(cv, cvs[cv] + self.deltaTE)
                else:
                    self.exsiInstance.sendSetCV(cv, cvs[cv])
            self.exsiInstance.sendPatientTable()
            if not self.autoPrescanDone:
                self.exsiInstance.prescanDone.clear()
                self.exsiInstance.sendPrescan(True)
                self.autoPrescanDone = True
                self.exsiInstance.send(
                    "GetPrescanValues"
                )  # get the center frequency and the linear gradients that are set
            else:
                self.exsiInstance.sendPrescan(False)
            self.exsiInstance.sendScan()

    def queueFieldmapProtocol(self):
        """
        Queue a two scans to complete a B0Map. This just loads the protocol.
        """
        self.exsiInstance.sendLoadProtocol("ConformalShimCalibration3")

    def queueB0MapPairScan(self):
        """
        Queue all the commands to perform the basis pair scan
        -prescan: bool, whether to do an auto prescan -- This will reset the linear gradients!
        """
        # Basic basis pair scan. should be used to scan the background
        self.exsiInstance.sendLoadProtocol("ConformalShimCalibration3")
        self.queueTwoFgreSequences()

    def countScansCompleted(self, n):
        """should be 2 for every basis pair scan"""
        for i in range(n):
            self.log(f"Checking for failure on last run; On scan {i+1} / {n}")
            if not self.exsiInstance.no_failures.is_set():
                self.log("Error: scan failed")
                self.exsiInstance.no_failures.set()
                return False
            self.log(f"No Fail. Waiting for scan to complete, On scan {i+1} / {n}")
            if not self.exsiInstance.images_ready_event.wait(timeout=90):
                self.log(f"Error: scan {i+1} / {n} didn't complete within 90 seconds bruh")
                return False
            else:
                self.exsiInstance.images_ready_event.clear()
                # TODO probably should raise some sorta error here...
        self.log(f"Done. {n} scans completed!")
        # after scans get completed, go ahead and get the latest scan data over on this machine...
        self.transferScanData()
        return True

    def requireShimConnection(func):
        """Decorator to check if the EXSI client is connected before running a function."""

        def wrapper(self, *args, **kwargs):
            # Check the status of the event
            if not self.shimInstance.connectedEvent.is_set() and not self.debugging:
                self.log(
                    "SHIM Client Not Connected!"
                    + "The SHIM client is still not connected to shim arduino."
                    + "Closing Client.\nCheck that arduino is connected to the HV Computer via USB.\n"
                    + "Check that the arduino port is set correctly using serial_finder.sh script."
                )
                return
            return func(self, *args, **kwargs)

        return wrapper

    def requireExsiConnection(func):
        """Decorator to check if the EXSI client is connected before running a function."""

        def wrapper(self, *args, **kwargs):
            # Check the status of the event
            if not self.exsiInstance.connected_ready_event.is_set() and not self.debugging:
                self.log(
                    "EXSI Client Not Connected!"
                    + "The EXSI client is still not connected to scanner."
                    + "Closing Client.\nCheck that External Host on scanner computer set to 'newHV'."
                )
                return
            return func(self, *args, **kwargs)

        return wrapper

    def requireAssetCalibration(func):
        """Decorator to check if the ASSET calibration scan is done before running a function."""

        def wrapper(self, *args, **kwargs):
            # TODO(rob): probably better to figure out how to look at existing scan state. somehow check all performed scans on start?
            if not self.assetCalibrationDone and not self.debugging:
                self.log("Debug: Need to do calibration scan before running scan with ASSET.")
                return
            return func(self, *args, **kwargs)

        return wrapper

    # ----------- Shim Tool Scan Functions ----------- #

    @requireExsiConnection
    def doCalibrationScan(self, trigger: Trigger = None):
        if self.exsiInstance and not self.assetCalibrationDone:
            self.exsiInstance.sendLoadProtocol("ConformalShimCalibration4")  # TODO rename the sequences on the scanner
            self.exsiInstance.sendSelTask()
            self.exsiInstance.sendActTask()
            self.exsiInstance.sendPatientTable()
            self.exsiInstance.sendScan()
            if self.exsiInstance.images_ready_event.wait(timeout=120):
                self.assetCalibrationDone = True
                self.exsiInstance.images_ready_event.clear()
                self.transferScanData()
                self.getLatestData(stride=1)
        if trigger is not None:
            trigger.finished.emit()

    @requireExsiConnection
    @requireAssetCalibration
    def doFgreScan(self, trigger: Trigger = None):
        if self.exsiInstance:
            self.exsiInstance.sendLoadProtocol("ConformalShimCalibration5")
            self.exsiInstance.sendSelTask()
            self.exsiInstance.sendActTask()
            self.exsiInstance.sendPatientTable()
            self.exsiInstance.sendScan()
            if not self.exsiInstance.images_ready_event.wait(timeout=120):
                self.log(f"scan didn't complete")
            else:
                self.exsiInstance.images_ready_event.clear()
                self.transferScanData()
                self.getLatestData(stride=1)
        if trigger is not None:
            trigger.finished.emit()

    @requireExsiConnection
    def getAndSetROIImage(self, getBackground: bool = False, trigger: Trigger = None):
        if getBackground == 1:
            self.log("Getting background image")
            self.getROIBackgound()
        else:
            self.log("Getting latest image")
            self.transferScanData()
            if os.path.exists(self.localExamRootDir):
                self.getLatestData(stride=1)
            else:
                self.log("local directory has not been made yet...")
        if trigger is not None:
            trigger.finished.emit()

    def recomputeCurrentsAndView(self, trigger: Trigger = None):
        self.computeMask()
        self.applyMask()
        if self.backgroundB0Map is not None and self.basisB0maps[0] is not None:
            self.expectedB0Map = None
            self.computeShimCurrents()
        self.evaluateShimImages()
        if trigger is not None:
            trigger.finished.emit()

    @requireExsiConnection
    @requireShimConnection
    @requireAssetCalibration
    def doFieldmapScan(self, trigger: Trigger = None, sliceIdx=None):
        """
        Perform a fieldmap scan to get the background b0 map.
        This will perform a fieldmap as is currently set in the scanner.
        """

        obtainPrinciples = False
        if not self.autoPrescanDone:
            self.resetPrincipleSols()
            self.resetShimSols()
            self.shimInstance.shimZero()
            obtainPrinciples = True

        # need to kick this off in a new thread because it waits for the prescan to be completed if it is doing a prescan
        kickoff_thread(self.queueB0MapPairScan)

        self.exsiInstance.images_ready_event.clear()
        if self.countScansCompleted(2):
            self.transferScanData()

            if self.backgroundB0Map is not None:
                self.computeShimmedB0Map(sliceIdx)
            else:
                self.computeBackgroundB0map()

            # should set the principle solutions to the current center frequency and linear gradients
            if obtainPrinciples:
                self.resetPrincipleSols()

            self.evaluateShimImages()
            if trigger is not None:
                trigger.success = True

        else:
            self.log("Error: Scans didn't complete")
            self.exsiInstance.images_ready_event.clear()
            self.exsiInstance.ready_event.clear()
        if trigger is not None:
            trigger.finished.emit()

    @requireExsiConnection
    @requireShimConnection
    @requireAssetCalibration
    def doBasisCalibrationScans(self, trigger: Trigger = None):
        """Perform all the calibration scans for each basis in the shim system."""

        def queueAll():
            # perform the calibration scans for the linear gradients
            for i in range(3):
                linGrad = [0.0, 0.0, 0.0]
                linGrad[i] = self.gradientCalStrength
                self.queueFieldmapProtocol()
                self.setLinGradients(linGrad=linGrad)
                self.queueTwoFgreSequences()

            for i in range(self.shimInstance.numLoops):
                self.queueFieldmapProtocol()
                self.sendSyncedLoopSolution(i, sliceIdx=None, ZeroOthers=True, calibration=True)
                self.setLinGradients()  # set linear gradients down to zero
                self.queueTwoFgreSequences()

        kickoff_thread(queueAll)

        self.shimInstance.shimZero()  # NOTE: Hopefully this zeros quicker that the scans get set up...
        self.rawBasisB0maps = None
        self.exsiInstance.images_ready_event.clear()
        num_scans = (self.shimInstance.numLoops + 3) * 2
        if self.countScansCompleted(num_scans):
            self.log("DEBUG: just finished all the calibration scans")
            self.computeBasisB0maps()
            # if this is a new background scan and basis maps were obtained, then compute the shim currents
            self.expectedB0Map = None
            success = self.computeShimCurrents()
            if trigger is not None:
                trigger.success = success  # set the success of operation
            self.evaluateShimImages()
        else:
            self.log("Error: Scans didn't complete")
            self.exsiInstance.images_ready_event.clear()
            self.exsiInstance.ready_event.clear()
        if trigger is not None:
            trigger.finished.emit()

    @requireShimConnection
    def setAllShimCurrents(self, sliceIdx, trigger: Trigger = None):
        """Set all the shim currents to the values that were computed and saved in the solutions array, for the specified slice"""

        if self.obtainedSolutions():
            if self.shimModes[self.shimMode] == "Slice-Wise" and self.solutions[sliceIdx] is not None:
                self.log("setting shim but passing here")
                pass
            # setting center frequency
            self.setCenterFrequency(deltaCF=self.solutionValuesToApply[sliceIdx][0])

            # setting the linear shims
            self.setLinGradients(linGrad=self.solutionValuesToApply[sliceIdx][1:4])

            # setting the loop shim currents
            for i in range(self.shimInstance.numLoops):
                self.sendSyncedLoopSolution(i, sliceIdx=sliceIdx)

        if trigger is not None:
            trigger.finished.emit()

    @requireExsiConnection
    @requireShimConnection
    @requireAssetCalibration
    def doEvalAppliedShims(self, sliceIdx, trigger: Trigger = None):
        if self.solutions[sliceIdx] is None:
            self.log("Error: No solutions computed for this slice.")
            return

        def queueAll():
            # perform the calibration scans for the linear gradients
            deltaCF = self.solutionValuesToApply[sliceIdx][0]
            self.setCenterFrequency(deltaCF)
            self.setLinGradients()
            self.queueB0MapPairScan()

            # need to wait for the previous scan to return all the images before doing this...
            self.exsiInstance.sendWaitForImagesCollected()
            self.setCenterFrequency(0)

            apply = self.solutionValuesToApply[sliceIdx]
            for i in range(3):
                linGrad = [0.0, 0.0, 0.0]
                linGrad[i] = apply[1 + i]
                self.queueFieldmapProtocol()
                self.setLinGradients(linGrad)
                self.queueTwoFgreSequences()

            for i in range(self.shimInstance.numLoops):
                self.queueFieldmapProtocol()
                self.sendSyncedLoopSolution(i, sliceIdx=sliceIdx, ZeroOthers=True)
                self.setLinGradients()
                self.queueTwoFgreSequences()

        kickoff_thread(queueAll)

        self.shimInstance.shimZero()
        self.exsiInstance.images_ready_event.clear()
        num_scans = (self.shimInstance.numLoops + 4) * 2
        if self.countScansCompleted(num_scans):
            self.log("DEBUG: just finished all the shim eval scans")
            self.evaluateAppliedShims(sliceIdx)
        else:
            self.log("Error: Scans didn't complete")
            self.exsiInstance.images_ready_event.clear()
            self.exsiInstance.ready_event.clear()
        if trigger:
            trigger.finished.emit()

    @requireExsiConnection
    @requireShimConnection
    @requireAssetCalibration
    def doAllShimmedScans(self, trigger: Trigger = None):
        if self.expectedB0Map is not None:
            if trigger is not None:
                trigger.finished.emit()
            return

        self.log(f"DEBUG: ________________________Do All Shim Scans____________________________________")
        # compute how many scans needed, i.e. how many slices are not Nans out of the ROI
        startIdx = None
        numindex = 0
        for i in range(self.backgroundB0Map.shape[1]):
            if self.shimStats[1][i] is not None:
                numindex += 1
                if startIdx is None:
                    startIdx = i

        if numindex == 0:
            self.log("Error: No slices to shim")
            if trigger is not None:
                trigger.finished.emit()
            return

        self.log(f"DEBUG: Starting at index {startIdx} and doing {numindex} B0MAPS")
        self.exsiInstance.images_ready_event.clear()

        def queueAll():
            for i in range(startIdx, startIdx + numindex):
                if i > startIdx:
                    self.exsiInstance.sendWaitForImagesCollected()
                self.setAllShimCurrents(i)  # set all of the currents
                self.queueB0MapPairScan()

        kickoff_thread(queueAll)

        for idx in range(startIdx, startIdx + numindex):
            self.log(f"-------------------------------------------------------------")
            self.log(f"DEBUG: STARTING B0MAP {idx-startIdx+1} / {numindex}; slice {idx}")
            self.log(f"DEBUG: solutions for this slice are {self.solutions[idx]}")
            self.log(f"DEBUG: applied values for this slice are {self.solutionValuesToApply[idx]}")

            self.log(f"DEBUG: now waiting to actually perform the slice")
            if self.countScansCompleted(2):
                # perform the rest of these functions in another thread so that the shim setting doesn't lag behind too much
                def updateVals():
                    self.computeShimmedB0Map(idx)
                    self.evaluateShimImages()
                    if trigger is not None:
                        trigger.finished.emit()

                kickoff_thread(updateVals)
            else:
                self.log("Error: Scans didn't complete")
                self.exsiInstance.images_ready_event.clear()
                self.exsiInstance.ready_event.clear()

    def saveResults(self):
        def helper():
            if not self.backgroundB0Map.any():
                # if the background b0map is not computed, then nothing else is and you can just return
                return
            self.log("saving Images")

            # get the time and date
            dt = datetime.now()
            dt = dt.strftime("%Y%m%d_%H%M%S")

            self.resultsDir = os.path.join(self.config["rootDir"], "results", self.exsiInstance.examNumber, dt)
            if not os.path.exists(self.resultsDir):
                os.makedirs(self.resultsDir)
            self.log(f"Saving results to {self.resultsDir}")

            # pack all the data into one easy to work with numpy array
            data = []
            bases = []
            labels = ["Background", "Expected", "Shimmed"]

            lastNotNone = 0
            vmax = 0
            # apply mask to all of the data
            self.computeMask()
            refs = [self.backgroundB0Map, self.expectedB0Map, self.shimmedB0Map]
            for i in range(3):
                if refs[i] is not None:
                    data.append(np.copy(refs[i]))
                    lastNotNone = i
                    data[i][~self.finalMask] = np.nan
                    vmax = max(vmax, np.nanmax(np.abs(data[i])))

            data = data[: lastNotNone + 1]  # only save data that has been collected so far (PRUNE THE NONEs)

            for i in range(len(bases)):
                if self.basisB0maps[i] is not None:
                    bases.append(np.copy(self.basisB0maps))
                    lastNotNone = i
                    bases[i][~self.finalMask] = np.nan
                    vmax = max(vmax, np.nanmax(np.abs(bases[i])))

            bases = bases[: lastNotNone + 1]  # only save basismaps that have been collected so far (PRUNE THE NONEs)

            # save individual images and stats
            for i in range(len(data)):
                imageTypeSaveDir = os.path.join(self.resultsDir, labels[i])
                imagesDir = os.path.join(imageTypeSaveDir, "images")
                histDir = os.path.join(imageTypeSaveDir, "histograms")
                for d in [imageTypeSaveDir, imagesDir, histDir]:
                    if not os.path.exists(d):
                        os.makedirs(d)

                self.log(f"Saving slice images and histograms for {labels[i]}")
                for j in range(data[0].shape[1]):
                    # save a perslice B0Map image and histogram
                    if not np.isnan(data[i][:, j, :]).all():
                        saveImage(imagesDir, labels[i], data[i][:, j, :], j, vmax)
                        saveHistogram(histDir, labels[i], data[i][:, j, :], j)

                self.log(f"Saving stats for {labels[i]}")
                # save all the slicewise stats, appended into one file
                saveStats(imageTypeSaveDir, labels[i], self.shimStatStrs[i])
                # generate and then save Volume wise stats
                stats, statarr = evaluate(data[i].flatten(), self.debugging)
                saveStats(imageTypeSaveDir, labels[i], stats, volume=True)

                self.log(f"Saving Volume stats for {labels[i]}")
                # save Volume wise histogram
                saveHistogram(imageTypeSaveDir, labels[i], data[i], -1)

            for i in range(len(bases)):
                if bases[i] is not None:
                    basesDir = os.path.join(self.resultsDir, "basisMaps")
                    baseDir = os.path.join(basesDir, f"basis{i}")
                    for d in [basesDir, baseDir]:
                        if not os.path.exists(d):
                            os.makedirs(d)
                    for j in range(bases[i].shape[1]):
                        if not np.isnan(bases[i][:, j, :]).all():
                            saveImage(baseDir, f"basis{i}", bases[i][:, j, :], j, vmax)

            # save the histogram  all images overlayed
            if len(data) >= 2:
                self.log(f"Saving overlayed Volume stats for ROI")
                data = np.array(data)
                # for the Volume entirely
                saveHistogramsOverlayed(self.resultsDir, labels, data, -1)
                # for each slice independently
                overlayHistogramDir = os.path.join(self.resultsDir, "overlayedHistogramPerSlice")
                if not os.path.exists(overlayHistogramDir):
                    os.makedirs(overlayHistogramDir)
                for j in range(data[0].shape[1]):
                    if not np.isnan(data[0][:, j, :]).all():
                        saveHistogramsOverlayed(overlayHistogramDir, labels, data[:, :, j, :], j)

            # save the numpy data
            np.save(os.path.join(self.resultsDir, "shimData.npy"), data)

            np.save(os.path.join(self.resultsDir, "basis.npy"), bases)
            self.log(f"Done saving results to {self.resultsDir}")

        kickoff_thread(helper)

    # ----------- random methods ------------ #

    def log(self, message):
        """Log a message."""
        header = "SHIM TOOL: "
        log(header + message, self.debugging)
