# =============================================================================
# Copyright (C) 2010 Diego Duclos
#
# This file is part of pyfa.
#
# pyfa is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pyfa is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with pyfa.  If not, see <http://www.gnu.org/licenses/>.
# =============================================================================


import itertools
import math
import os
import traceback
from bisect import bisect


# noinspection PyPackageRequirements
import wx
from logbook import Logger


from graphs.style import BASE_COLORS, LIGHTNESSES, STYLES, hsl_to_hsv
from gui.utils.numberFormatter import roundToPrec


pyfalog = Logger(__name__)


def _filterLowYValues(xs, ys, minY=1, addZeroPoint=False):
    """
    Filter out trailing points where Y < minY (default 1).
    
    For damage graphs, values less than 1 are effectively zero.
    Only filters trailing low-Y values - keeps low-Y values in the middle if there are
    valid Y>=minY points at further ranges.
    
    Returns filtered (xs, ys) lists.
    If addZeroPoint=True AND filtering actually removed trailing low-Y points,
    adds a final point at Y=0 to connect to the axis.
    Note: Does NOT add Y=0 if data ends at user-specified bounds (no trailing low-Y values).
    """
    if not xs or not ys:
        return xs, ys
    
    # Find the last index where Y >= minY
    lastValidIdx = -1
    for i in range(len(ys) - 1, -1, -1):
        if ys[i] >= minY:
            lastValidIdx = i
            break
    
    # If no valid points, return empty
    if lastValidIdx < 0:
        return [], []
    
    # Keep all points up to and including the last valid point
    filteredXs = list(xs[:lastValidIdx + 1])
    filteredYs = list(ys[:lastValidIdx + 1])
    
    # Only add Y=0 point if filtering actually removed trailing low-Y points
    # (i.e., there's a point after lastValidIdx that was below minY)
    # This ensures we don't add Y=0 when data just ends at user bounds
    if addZeroPoint and lastValidIdx + 1 < len(xs):
        nextX = xs[lastValidIdx + 1]
        nextY = ys[lastValidIdx + 1]
        prevX = filteredXs[-1]
        prevY = filteredYs[-1]
        
        # Linear interpolation: find X where Y = minY (or close to 0)
        if prevY != nextY:
            crossX = prevX + (minY - prevY) * (nextX - prevX) / (nextY - prevY)
            filteredXs.append(crossX)
            filteredYs.append(0)
    # Note: Removed the 'elif addZeroPoint and filteredXs' branch
    # We should NOT add Y=0 if the data simply ends at the last point
    # (no trailing low-Y values were filtered out)
    
    return filteredXs, filteredYs


try:
    import matplotlib as mpl

    mpl_version = int(mpl.__version__[0]) or -1
    if mpl_version >= 2:
        mpl.use('wxagg')
        graphFrame_enabled = True
    else:
        graphFrame_enabled = False

    from matplotlib.lines import Line2D
    from matplotlib.backends.backend_wxagg import FigureCanvasWxAgg as Canvas
    from matplotlib.figure import Figure
    from matplotlib.colors import hsv_to_rgb
    import matplotlib.patheffects as PathEffects
except ImportError as e:
    pyfalog.warning('Matplotlib failed to import.  Likely missing or incompatible version.')
    graphFrame_enabled = False
except (KeyboardInterrupt, SystemExit):
    raise
except Exception:
    # We can get exceptions deep within matplotlib. Catch those.  See GH #1046
    tb = traceback.format_exc()
    pyfalog.critical('Exception when importing Matplotlib. Continuing without importing.')
    pyfalog.critical(tb)
    graphFrame_enabled = False


class GraphCanvasPanel(wx.Panel):

    def __init__(self, graphFrame, parent):
        super().__init__(parent)
        self.graphFrame = graphFrame

        # Remove matplotlib font cache, see #234
        try:
            cache_dir = mpl._get_cachedir()
        except (KeyboardInterrupt, SystemExit):
            raise
        except:
            cache_dir = os.path.expanduser(os.path.join('~', '.matplotlib'))
        cache_file = os.path.join(cache_dir, 'fontList.cache')
        if os.access(cache_dir, os.W_OK | os.X_OK) and os.path.isfile(cache_file):
            os.remove(cache_file)

        mainSizer = wx.BoxSizer(wx.VERTICAL)

        self.figure = Figure(figsize=(5, 3), tight_layout={'pad': 1.08})
        rgbtuple = wx.SystemSettings.GetColour(wx.SYS_COLOUR_BTNFACE).Get()
        clr = [c / 255. for c in rgbtuple]
        self.figure.set_facecolor(clr)
        self.figure.set_edgecolor(clr)
        self.canvas = Canvas(self, -1, self.figure)
        self.canvas.SetBackgroundColour(wx.Colour(*rgbtuple))
        self.canvas.mpl_connect('button_press_event', self.OnMplCanvasClick)
        self.subplot = self.figure.add_subplot(111)
        self.subplot.grid(True)
        mainSizer.Add(self.canvas, 1, wx.EXPAND | wx.ALL, 0)

        self.SetSizer(mainSizer)

        self.xMark = None
        self.mplOnDragHandler = None
        self.mplOnReleaseHandler = None

    def draw(self, accurateMarks=True):
        self.subplot.clear()
        self.subplot.grid(True)
        allXs = set()
        allYs = set()
        plotData = {}
        legendData = []
        chosenX = self.graphFrame.ctrlPanel.xType
        chosenY = self.graphFrame.ctrlPanel.yType
        self.subplot.set(
            xlabel=self.graphFrame.ctrlPanel.formatLabel(chosenX),
            ylabel=self.graphFrame.ctrlPanel.formatLabel(chosenY))

        mainInput, miscInputs = self.graphFrame.ctrlPanel.getValues()
        view = self.graphFrame.getView()
        
        # Track the effective max X where data ends (where Y drops to minY threshold)
        # This is used to limit X bounds for missile-like data that doesn't span full range
        effectiveMaxX = None
        
        # Set ammo quality on view for segmented graphs
        if hasattr(view, 'hasSegments') and view.hasSegments:
            view._ammoQuality = self.graphFrame.ctrlPanel.ammoQuality
        
        sources = self.graphFrame.ctrlPanel.sources
        if view.hasTargets:
            iterList = tuple(itertools.product(sources, self.graphFrame.ctrlPanel.targets))
        else:
            iterList = tuple((f, None) for f in sources)

        # Check if this view supports segmented plotting
        hasSegments = getattr(view, 'hasSegments', False)

        # Draw plot lines and get data for legend
        for source, target in iterList:
            # Get line style data
            try:
                colorData = BASE_COLORS[source.colorID]
            except KeyError:
                pyfalog.warning('Invalid color "{}" for "{}"'.format(source.colorID, source.name))
                continue
            baseColor = colorData.hsl
            lineStyle = 'solid'
            if target is not None:
                try:
                    lightnessData = LIGHTNESSES[target.lightnessID]
                except KeyError:
                    pyfalog.warning('Invalid lightness "{}" for "{}"'.format(target.lightnessID, target.name))
                    continue
                baseColor = lightnessData.func(baseColor)
                try:
                    lineStyleData = STYLES[target.lineStyleID]
                except KeyError:
                    pyfalog.warning('Invalid line style "{}" for "{}"'.format(target.lightnessID, target.name))
                    continue
                lineStyle = lineStyleData.mplSpec

            # Try segmented plotting first if supported
            segmentsPlotted = False
            if hasSegments:
                try:
                    segments = view.getPlotSegments(
                        mainInput=mainInput,
                        miscInputs=miscInputs,
                        xSpec=chosenX,
                        ySpec=chosenY,
                        src=source,
                        tgt=target)
                    # Debug: log segment info
                    if segments:
                        pyfalog.debug('Segments for {} vs {}: {} segments'.format(
                            source.name, target.name if target else 'None', len(segments)))
                        for i, seg in enumerate(segments):
                            pyfalog.debug('  Segment {}: ammo={}, x_range=[{:.0f}, {:.0f}], y_range=[{:.0f}, {:.0f}]'.format(
                                i, seg.get('ammo'), min(seg['xs']), max(seg['xs']), min(seg['ys']), max(seg['ys'])))
                    if segments:
                        segmentsPlotted = True
                        # Base color from source/target selection
                        baseRgbColor = hsv_to_rgb(hsl_to_hsv(baseColor))
                        styleKeys = list(STYLES.keys())
                        
                        # Get ammo style from control panel ('none', 'pattern', 'color')
                        ammoStyle = self.graphFrame.ctrlPanel.ammoStyle
                        getAmmoColorFunc = getattr(view, 'getAmmoColor', None)
                        
                        segmentXs = []
                        segmentYs = []
                        legendSegments = []  # Track segments for legend
                        lastSegmentColor = None
                        lastSegmentStyle = None
                        lastSegmentMaxX = None
                        
                        for segIdx, segment in enumerate(segments):
                            xs = segment['xs']
                            ys = segment['ys']
                            ammoName = segment.get('ammo', 'Unknown')
                            ammoIndex = segment.get('ammoIndex', 0)
                            
                            if not self.__checkNumbers(xs, ys):
                                continue
                            
                            # Check if this is the last segment
                            isLastSegment = (segIdx == len(segments) - 1)
                            
                            # Filter out points where Y < 1 (effectively zero damage)
                            # Add Y=0 point only for the last segment to connect to axis
                            xs, ys = _filterLowYValues(xs, ys, minY=1, addZeroPoint=isLastSegment)
                            if not xs or not ys:
                                continue
                            
                            # Track effective max X (where data actually ends)
                            if xs:
                                segMaxX = max(xs)
                                if effectiveMaxX is None or segMaxX > effectiveMaxX:
                                    effectiveMaxX = segMaxX
                            
                            # Determine color and line style based on ammo style mode
                            if ammoStyle == 'color' and getAmmoColorFunc:
                                # Color mode: use ammo-specific colors, use target's line style
                                ammoColor = getAmmoColorFunc(ammoName)
                                if ammoColor:
                                    segColor = ammoColor
                                else:
                                    # Fallback to base color if no ammo color defined
                                    segColor = baseRgbColor
                                # Use the target's line style selection
                                segLineStyle = lineStyle
                            elif ammoStyle == 'pattern':
                                # Pattern mode: use base color, vary line patterns
                                segColor = baseRgbColor
                                segStyleKey = styleKeys[ammoIndex % len(styleKeys)]
                                segStyleData = STYLES[segStyleKey]
                                segLineStyle = segStyleData.mplSpec
                            else:
                                # None mode: solid single color line
                                segColor = baseRgbColor
                                segLineStyle = 'solid'
                            
                            # Track last segment info for potential Y=0 connection
                            lastSegmentColor = segColor
                            lastSegmentStyle = segLineStyle
                            lastSegmentMaxX = max(xs) if xs else None
                            
                            # Plot this segment
                            if len(xs) == 1 and len(ys) == 1:
                                self.subplot.plot(xs, ys, color=segColor, linestyle=segLineStyle, marker='.', linewidth=2)
                            else:
                                self.subplot.plot(xs, ys, color=segColor, linestyle=segLineStyle, linewidth=2)
                            
                            segmentXs.extend(xs)
                            segmentYs.extend(ys)
                            
                            # Track for legend (color mode only) - always use solid lines in legend
                            if ammoStyle == 'color' and ammoName not in [ls[2] for ls in legendSegments]:
                                legendSegments.append((segColor, 'solid', ammoName))
                        
                        # Store combined data for X mark lookup
                        if segmentXs and segmentYs:
                            plotData[(source, target)] = (segmentXs, segmentYs)
                            allXs.update(segmentXs)
                            allYs.update(segmentYs)
                        
                        # Add legend entries
                        if ammoStyle == 'color':
                            # Add legend entry for each ammo type (avoid duplicates across targets)
                            existingLabels = [ld[2] for ld in legendData]
                            for segColor, segLineStyle, ammoName in legendSegments:
                                if ammoName not in existingLabels:
                                    legendData.append((segColor, 'solid', ammoName))
                                    existingLabels.append(ammoName)
                        else:
                            # Single legend entry for this source (none or pattern mode)
                            if target is None:
                                legendData.append((baseRgbColor, 'solid', source.shortName))
                            else:
                                legendData.append((baseRgbColor, 'solid', '{} vs {}'.format(source.shortName, target.shortName)))
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception as e:
                    pyfalog.warning('Failed to get segments for "{}" vs "{}": {}'.format(
                        source.name, '' if target is None else target.name, e))

            # Fall back to regular plotting if segments not available or failed
            if not segmentsPlotted:
                color = hsv_to_rgb(hsl_to_hsv(baseColor))
                try:
                    xs, ys = view.getPlotPoints(
                        mainInput=mainInput,
                        miscInputs=miscInputs,
                        xSpec=chosenX,
                        ySpec=chosenY,
                        src=source,
                        tgt=target)
                    if not self.__checkNumbers(xs, ys):
                        pyfalog.warning('Failed to plot "{}" vs "{}" due to inf or NaN in values'.format(source.name, '' if target is None else target.name))
                        continue
                    # Filter out Y values below 1 (damage can't be less than 1)
                    # Add Y=0 point to connect line to axis
                    xs, ys = _filterLowYValues(xs, ys, addZeroPoint=True)
                    if not xs or not ys:
                        continue
                    
                    # Track effective max X (where data actually ends)
                    if xs:
                        dataMaxX = max(xs)
                        if effectiveMaxX is None or dataMaxX > effectiveMaxX:
                            effectiveMaxX = dataMaxX
                    
                    plotData[(source, target)] = (xs, ys)
                    allXs.update(xs)
                    allYs.update(ys)
                    # If we have single data point, show marker - otherwise line won't be shown
                    if len(xs) == 1 and len(ys) == 1:
                        self.subplot.plot(xs, ys, color=color, linestyle=lineStyle, marker='.')
                    else:
                        self.subplot.plot(xs, ys, color=color, linestyle=lineStyle)
                    # Fill data for legend
                    if target is None:
                        legendData.append((color, lineStyle, source.shortName))
                    else:
                        legendData.append((color, lineStyle, '{} vs {}'.format(source.shortName, target.shortName)))
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception:
                    pyfalog.warning('Failed to plot "{}" vs "{}"'.format(source.name, '' if target is None else target.name))
                    self.canvas.draw()
                    self.Refresh()
                    return

        # Setting Y limits for canvas (always include Y=0 in range)
        allYs.add(0)
        # Include the user's input range in X limits so axis extends to full range
        if mainInput and mainInput.value:
            allXs.add(min(mainInput.value))
            # Only add max if we don't have an effective max from filtered data
            if effectiveMaxX is None:
                allXs.add(max(mainInput.value))
            else:
                # Use effective max X * 1.1 for bounds (where data actually ends)
                effectiveMaxXWithMargin = effectiveMaxX * 1
                allXs.add(effectiveMaxXWithMargin)
        canvasMinY, canvasMaxY = self._getLimits(allYs, minExtra=0.05, maxExtra=0.1, roundNice=True)
        canvasMinX, canvasMaxX = self._getLimits(allXs, minExtra=0.02, maxExtra=0.02, roundNice=False)
        # Clamp Y minimum to 0 - damage values can't be negative
        canvasMinY = max(0, canvasMinY)
        self.subplot.set_ylim(bottom=canvasMinY, top=canvasMaxY)
        self.subplot.set_xlim(left=canvasMinX, right=canvasMaxX)
        # Process X marks line
        if self.xMark is not None:
            minX = min(allXs, default=None)
            maxX = max(allXs, default=None)
            if minX is not None and maxX is not None:
                minY = min(allYs, default=None)
                maxY = max(allYs, default=None)
                yDiff = (maxY or 0) - (minY or 0)
                xMark = max(min(self.xMark, maxX), minX)
                
                # Draw line first
                self.subplot.axvline(x=xMark, linestyle='dotted', linewidth=1, color=(0, 0, 0))
                
                # Prepare X label text (without prefix/suffix yet)
                if chosenX.unit is None:
                    xLabelCore = '{}'.format(roundToPrec(xMark, 4))
                else:
                    xLabelCore = '{} {}'.format(roundToPrec(xMark, 4), chosenX.unit)
                
                # Text outline effect for better visibility
                textOutline = [PathEffects.withStroke(linewidth=3, foreground='white')]
                
                # Get Y values with optional extra info (like ammo name)
                yMarks = {}  # {rounded_value: extra_info_str}

                def addYMark(val, extraInfo=None):
                    if val is None:
                        return
                    # Round according to shown Y range - the bigger the range,
                    # the rougher the rounding
                    if yDiff != 0:
                        rounded = roundToPrec(val, 4, nsValue=yDiff)
                    else:
                        rounded = val
                    # If due to some bug or insufficient plot density we're
                    # out of bounds, do not add anything
                    if minY <= val <= maxY or minY <= rounded <= maxY:
                        yMarks[rounded] = extraInfo

                for source, target in iterList:
                    if (source, target) not in plotData:
                        continue
                    xs, ys = plotData[(source, target)]
                    if not xs or xMark < min(xs) or xMark > max(xs):
                        continue
                    # Fetch values from graphs when we're asked to provide accurate data
                    if accurateMarks:
                        try:
                            # Try extended point info first (for ammo name etc.)
                            if hasattr(view, 'getPointExtended'):
                                y, extraInfo = view.getPointExtended(
                                    x=xMark,
                                    miscInputs=miscInputs,
                                    xSpec=chosenX,
                                    ySpec=chosenY,
                                    src=source,
                                    tgt=target)
                                # Build extra info string
                                extraStr = None
                                if extraInfo and extraInfo.get('ammo'):
                                    extraStr = extraInfo['ammo']
                                addYMark(y, extraStr)
                            else:
                                y = view.getPoint(
                                    x=xMark,
                                    miscInputs=miscInputs,
                                    xSpec=chosenX,
                                    ySpec=chosenY,
                                    src=source,
                                    tgt=target)
                                addYMark(y)
                        except (KeyboardInterrupt, SystemExit):
                            raise
                        except Exception:
                            pyfalog.warning('Failed to get X mark for "{}" vs "{}"'.format(source.name, '' if target is None else target.name))
                            # Silently skip this mark, otherwise other marks and legend display will fail
                            continue
                    # Otherwise just do linear interpolation between two points
                    else:
                        # Try fast ammo name lookup (O(log n) using cached transitions)
                        extraStr = None
                        if hasattr(view, 'getAmmoNameFast'):
                            try:
                                extraStr = view.getAmmoNameFast(x=xMark, xSpec=chosenX, src=source, tgt=target)
                            except (KeyboardInterrupt, SystemExit):
                                raise
                            except Exception:
                                pass  # Silently ignore - just won't show ammo name
                        
                        if xMark in xs:
                            # We might have multiples of the same value in our sequence, pick value for the last one
                            idx = len(xs) - xs[::-1].index(xMark) - 1
                            addYMark(ys[idx], extraStr)
                            continue
                        idx = bisect(xs, xMark)
                        yMark = self._interpolateX(x=xMark, x1=xs[idx - 1], y1=ys[idx - 1], x2=xs[idx], y2=ys[idx])
                        addYMark(yMark, extraStr)

                # Draw Y values with optional extra info
                # First, collect all labels to determine the widest one
                labelData = []  # List of (yMark, labelText)
                
                # For DPS graphs (Damage Stats and Application Profile), show integers
                isDpsGraph = view.internalName in ('dmgStatsGraph', 'ammoOptimalDpsGraph')
                
                for yMark, extraInfo in yMarks.items():
                    # Format yMark as integer for DPS graphs
                    if isDpsGraph:
                        yMarkStr = '{:.0f}'.format(yMark)
                    else:
                        yMarkStr = '{}'.format(yMark)
                    
                    if extraInfo:
                        labelText = '{} ({})'.format(yMarkStr, extraInfo)
                    else:
                        labelText = yMarkStr
                    labelData.append((yMark, labelText))
                
                # Determine alignment based on position in data range
                # Use a simple percentage-based approach but factor in text length
                # by using a smaller threshold for longer text
                xRange = canvasMaxX - canvasMinX
                xPosRatio = (xMark - canvasMinX) / xRange if xRange > 0 else 0
                
                # Find the longest label to estimate how early we need to flip
                maxLabelLen = len(xLabelCore)
                for yMark, labelText in labelData:
                    maxLabelLen = max(maxLabelLen, len(labelText))
                
                # Adjust threshold based on label length
                # Short labels (< 15 chars): flip at 80%
                # Medium labels (15-30 chars): flip at 65%  
                # Long labels (> 30 chars): flip at 50%
                if maxLabelLen < 15:
                    flipThreshold = 0.80
                elif maxLabelLen < 30:
                    flipThreshold = 0.65
                else:
                    flipThreshold = 0.50
                
                if xPosRatio > flipThreshold:
                    labelAlignment = 'right'
                    labelPrefix = ''
                    labelSuffix = ' '
                else:
                    labelAlignment = 'left'
                    labelPrefix = ' '
                    labelSuffix = ''
                
                # Now draw all labels with the chosen alignment
                textOutline = [PathEffects.withStroke(linewidth=3, foreground='white')]
                
                # Draw X label
                xLabel = '{}{}{}'.format(labelPrefix, xLabelCore, labelSuffix)
                self.subplot.annotate(
                    xLabel, xy=(xMark, canvasMaxY - 0.01 * (canvasMaxY - canvasMinY)), xytext=(0, 0), annotation_clip=False,
                    textcoords='offset pixels', ha=labelAlignment, va='top', fontsize='small',
                    path_effects=textOutline)
                
                # Draw Y labels
                for yMark, labelText in labelData:
                    label = '{}{}{}'.format(labelPrefix, labelText, labelSuffix)
                    self.subplot.annotate(
                        label, xy=(xMark, yMark), xytext=(0, 0),
                        textcoords='offset pixels', ha=labelAlignment, va='center', fontsize='small',
                        path_effects=textOutline)

        legendLines = []
        for i, iData in enumerate(legendData):
            color, lineStyle, label = iData
            legendLines.append(Line2D([0], [0], color=color, linestyle=lineStyle, label=label.replace('$', r'\$')))

        if len(legendLines) > 0 and self.graphFrame.ctrlPanel.showLegend:
            legend = self.subplot.legend(handles=legendLines)
            for t in legend.get_texts():
                t.set_fontsize('small')
            for l in legend.get_lines():
                l.set_linewidth(1)

        self.canvas.draw()
        self.Refresh()

    def markXApproximate(self, x):
        if x is not None:
            self.xMark = x
            self.draw(accurateMarks=False)

    def unmarkX(self):
        self.xMark = None
        self.draw()

    @staticmethod
    def _roundToNice(val, direction='up'):
        """
        Round a value to a 'nice' number (1, 2, 5, or 10 multiplied by power of 10).
        This helps stabilize Y-axis limits and reduce flickering.
        
        Args:
            val: Value to round
            direction: 'up' to round up (for max), 'down' to round down (for min)
        """
        if val == 0:
            return 0
        
        sign = 1 if val >= 0 else -1
        val = abs(val)
        
        # Find the order of magnitude
        magnitude = 10 ** math.floor(math.log10(val))
        normalized = val / magnitude
        
        # Nice numbers: 1, 2, 5, 10
        nice_numbers = [1, 2, 5, 10]
        
        if direction == 'up':
            # Round up to next nice number
            for nice in nice_numbers:
                if normalized <= nice:
                    return sign * nice * magnitude
            return sign * 10 * magnitude
        else:
            # Round down to previous nice number
            for nice in reversed(nice_numbers):
                if normalized >= nice:
                    return sign * nice * magnitude
            return sign * magnitude
    
    @staticmethod
    def _getLimits(vals, minExtra=0, maxExtra=0, roundNice=False):
        minVal = min(vals, default=0)
        maxVal = max(vals, default=0)
        # Extend range a little for some visual space
        valRange = maxVal - minVal
        minVal -= valRange * minExtra
        maxVal += valRange * maxExtra
        # Extend by % of value if we show function of a constant
        if minVal == maxVal:
            minVal -= minVal * 0.05
            maxVal += minVal * 0.05
        # If still equal, function is 0, spread out visual space as special case
        if minVal == maxVal:
            minVal -= 5
            maxVal += 5
        # Round to nice values to reduce Y-axis flickering (only for Y-axis)
        if roundNice and maxVal > 0:
            maxVal = GraphCanvasPanel._roundToNice(maxVal * 1.02, 'up')  # Add 2% buffer before rounding
        return minVal, maxVal

    @staticmethod
    def _interpolateX(x, x1, y1, x2, y2):
        pos = (x - x1) / (x2 - x1)
        y = y1 + pos * (y2 - y1)
        return y

    @staticmethod
    def __checkNumbers(xs, ys):
        for number in itertools.chain(xs, ys):
            if math.isnan(number) or math.isinf(number):
                return False
        return True

    # Matplotlib event handlers
    def OnMplCanvasClick(self, event):
        if event.button == 1:
            if not self.mplOnDragHandler:
                self.mplOnDragHandler = self.canvas.mpl_connect('motion_notify_event', self.OnMplCanvasDrag)
            if not self.mplOnReleaseHandler:
                self.mplOnReleaseHandler = self.canvas.mpl_connect('button_release_event', self.OnMplCanvasRelease)
            self.markXApproximate(event.xdata)
        elif event.button == 3:
            self.unmarkX()

    def OnMplCanvasDrag(self, event):
        self.markXApproximate(event.xdata)

    def OnMplCanvasRelease(self, event):
        if event.button == 1:
            if self.mplOnDragHandler:
                self.canvas.mpl_disconnect(self.mplOnDragHandler)
                self.mplOnDragHandler = None
            if self.mplOnReleaseHandler:
                self.canvas.mpl_disconnect(self.mplOnReleaseHandler)
                self.mplOnReleaseHandler = None
            # Do not write markX here because of strange mouse behavior: when dragging,
            # sometimes when you release button, x coordinate changes. To avoid that,
            # we just re-use coordinates set on click/drag and just request to redraw
            # using accurate data
            self.draw(accurateMarks=True)
