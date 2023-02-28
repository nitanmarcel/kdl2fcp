#!/usr/bin/python3

#
# Kdenlive to FCP (Final Cut Pro, Davinci Resolve, Adobe Premiere, others) video project XML converter 
#
# (C) Gabriel Gambetta 2019.
#
# Modified by Guido Torelli and Marcel Alexandru Nitan
#
# http://gabrielgambetta.com (tech), http://gabrielgambetta.biz (filmmaking), http://twitter.com/gabrielgambetta
#
# Licensed under GPL v3.
#

import argparse
import os
import os.path
import glob
import re

from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET

# =============================================================================
#  Data model.
# =============================================================================


class ClipFile:
    def __init__(self, resource_path):
        self.resource_path = resource_path


class Clip:
    def __init__(self, clip_id, name, resource, duration):
        self.clip_id = clip_id
        self.name = name
        self.resource = resource
        self.duration = duration


class Entry:
    def __init__(self, clip, in_time, out_time):
        self.clip = clip
        self.in_time = in_time
        self.out_time = out_time


class Track:
    def __init__(self, is_audio):
        self.is_audio = is_audio
        self.entries = []

    def addEntry(self, entry):
        self.entries.append(entry)


global_embed_counter = 0


class Project:
    def __init__(self):
        self.clips = {}
        self.tracks = []
        self.frame_rate = None

        global global_embed_counter
        if global_embed_counter == 0:
            self.id_prefix = "ROOT_" if EMBEDDED_MLT_TO_COMPOUND_CLIP else ""
        else:
            self.id_prefix = "EMB_%02d_" % global_embed_counter

        global_embed_counter += 1

    def addClip(self, clip_id, clip):
        self.clips[clip_id] = clip

    def getClip(self, clip_id):
        return self.clips[clip_id]

    def addTrack(self, track):
        self.tracks.append(track)


# =============================================================================
#  Kdenlive reader.
# =============================================================================
def selectFirst(node, selector):
    values = node.select(selector)
    if values:
        return values[0]
    return None

# ==============================================================================
   # else:
    #    return float(time_str) / frame_rate


class KdenliveReader:

    def __init__(self, force_time_frames=False, force_time_timestamp=False, legacy_format=False):
        self.force_time_frames = force_time_frames if not force_time_timestamp else False
        self.force_time_timestamp = force_time_timestamp if not force_time_frames else False
        self.version = 0 if force_time_frames else 1
        self.legacy_format = legacy_format
        self._parseTime = self._parseTimeFrames if self.time_parse_type == "frames" else self._parseTimeStr

    def _parseTimeStr(self, time_str, frame_rate):
        pattern = re.compile(r"(\d\d):(\d\d):(\d\d).(\d\d\d)")
        match = pattern.match(time_str)
        hours = int(match.group(1))
        minutes = int(match.group(2))
        seconds = int(match.group(3))
        millis = int(match.group(4))
        return hours*3600 + minutes*60 + seconds + millis/1000.0

    def _parseTimeFrames(self, nr_frames, frame_rate):
        return float(nr_frames) / frame_rate

    def read(self, filename):
        self.soup = None
        with open(filename, "r") as input_file:
            content = input_file.read()
            if self.time_parse_type == "auto":
                if re.search(r"<property name=\"kdenlive:docproperties\.version\">0\.\d\d</property>", content, flags=re.M) or self.force_time_frames == True:
                    self._parseTime = self._parseTimeFrames
                    self.version = 0
            self.soup = BeautifulSoup(content, "lxml-xml")
        self.project = Project()

        self._parseSettings()
        self._parseProducers()
        self._parseTracks()

        return self.project

    def _parseSettings(self):
        profile = selectFirst(self.soup, "mlt > profile")
        self.project.frame_rate_num = int(profile["frame_rate_num"])
        self.project.frame_rate_den = int(profile["frame_rate_den"])
        self.project.frame_rate = float(
            self.project.frame_rate_num) / float(self.project.frame_rate_den)

        self.project.width = int(profile["width"])
        self.project.height = int(profile["height"])
        print("Project format is %d x %d @ %.2f" %
              (self.project.width, self.project.height, self.project.frame_rate))

    def _parseProducers(self):
        self.resource_path_to_canonical_clip = {}
        self.clip_id_to_canonical_clip_id = {}

        resource_root = selectFirst(self.soup, "mlt")["root"]

        producers = self.soup.select("producer")
        for producer in producers:
            clip_id = producer["id"]
            if clip_id == "black_track":
                continue
            clip_id = self.project.id_prefix + clip_id

            resource_name = selectFirst(
                producer, "property[name=resource]").text
            original = selectFirst(
                producer, "property[name=\"kdenlive:originalurl\"]")
            if original:
                resource_name = original.text

            duration = self._parseTime(producer["out"], self.project.frame_rate)

            if re.match(r"[0-9.]+:", resource_name):
                # TODO: preserve slow motion information somewhere.
                colon_idx = resource_name.find(":")
                resource_name = resource_name[colon_idx+1:]

            resource_path = os.path.join(resource_root, resource_name)
            if resource_path in self.resource_path_to_canonical_clip:
                canonical_clip = self.resource_path_to_canonical_clip[resource_path]
                self.clip_id_to_canonical_clip_id[clip_id] = canonical_clip
            else:
                clip_name = os.path.split(resource_path)[1]
                clip_name_attr = selectFirst(
                    producer, "property[name=\"kdenlive:clipname\"]")
                if clip_name_attr:
                    clip_name = clip_name_attr.text
                    if not clip_name:
                        clip_name = os.path.basename(resource_path)

                if EMBEDDED_MLT_TO_COMPOUND_CLIP and resource_path.endswith(".kdenlive"):
                    reader = KdenliveReader()
                    print("Reading embedded project:", resource_path)
                    resource = reader.read(resource_path)
                else:
                    resource = ClipFile(resource_path)

                clip = Clip(clip_id, clip_name, resource, duration)

                self.clip_id_to_canonical_clip_id[clip_id] = clip_id
                self.resource_path_to_canonical_clip[resource_path] = clip_id
                self.project.addClip(clip_id, clip)
                print(clip_id, "->", resource_path)

        print("Parsed", len(self.project.clips), "clips.")

    def _parseTracks(self):
        audio_playlist_ids = set()
        tractors = self.soup.select("tractor")
        for tractor in tractors:
            is_audio_track = selectFirst(
                tractor, "property[name=\"kdenlive:audio_track\"]")
            if is_audio_track:
                print(tractor["id"], "is audio")
                tracks = tractor.select("track")
                for track in tracks:
                    audio_playlist_ids.add(track["producer"])

        print("Audio playlists:", audio_playlist_ids)

        playlists = self.soup.select("playlist")

        for playlist in playlists:
            playlist_id = playlist["id"]
            if playlist_id == "main_bin":
                continue
            is_audio = playlist_id in audio_playlist_ids
            entry = selectFirst(playlist, "entry")
            if self.legacy_format or self.version == 0:
                if entry:
                    producers = self.soup.select("producer")
                    for producer in producers:
                        if producer["id"] == entry["producer"]:
                            video_index = selectFirst(producer, "property[name=video_index]")
                            if video_index:
                                is_audio = int(video_index.text) == -1

            track = Track(is_audio)

            print("Playlist:", playlist_id,
                  ("[audio]" if is_audio else "[video]"))
            for entry in playlist.contents:
                if entry.name == "blank":
                    # non so perche, ma questo sistema il problema della duration per i gaps
                    length = self._parseTime(
                        entry["length"], self.project.frame_rate) - (1.0 / self.project.frame_rate)
                    print("\tBlank:", length)
                    entry = Entry(None, 0, length)
                    track.addEntry(entry)
                elif entry.name == "entry":
                    producer = self.project.id_prefix + entry["producer"]
                    clip_id = self.clip_id_to_canonical_clip_id[producer]
                    clip = self.project.getClip(clip_id)
                    in_time = self._parseTime(
                        entry["in"], self.project.frame_rate)
                    out_time = self._parseTime(
                        entry["out"], self.project.frame_rate)
                    print("\t%s [%.3f - %.3f]" % (producer, in_time, out_time))
                    entry = Entry(clip, in_time, out_time)
                    track.addEntry(entry)

            if track.entries:
                self.project.addTrack(track)
    @property
    def time_parse_type(self):
        if self.force_time_timestamp:
            return "timestamp"
        elif self.force_time_frames:
            return "frames"
        return "auto"

# =============================================================================
#  FCP XML writer.
# =============================================================================
class FcpXmlWriter:
    def __init__(self, project, legacy_format):
        self.xml = BeautifulSoup(features="xml")
        self.added_embedded_resources = set()
        self.project = project
        self.legacy_format = legacy_format

    def write(self, filename):
        root = self._addTag(self.xml, "fcpxml", version="1.5")

        resources_tag = self._addTag(root, "resources")
        self._addFormats(resources_tag)
        self._addResources(resources_tag)

        library = self._addTag(root, "library")
        self._addLibrary(library)
        xml = self.xml.prettify()
        self.write_xml(xml, filename)

# =============================================================================
# Remove unwanted elements. Done as a separate function because it easier to do the built-in module.
# https://docs.python.org/3/library/xml.etree.elementtree.html
# ============================================================================

    def write_xml(self, xml, file_name):
        tree = ET.ElementTree(ET.fromstring(xml))
        if not self.legacy_format:
            root = tree.getroot()
            resources = root.findall("resources")[0]
            spine = root.findall("library/event/project/sequence/spine")[0]
            elms_to_remove = []
            for child in resources.iter():
                if child.attrib:
                    if child.attrib.get("name", "") == "black":
                        elms_to_remove.append(child)
            for elm in elms_to_remove:
                resources.remove(elm)
            elms_to_remove = []
            for child in spine.iter():
                if child.attrib:
                    elms_to_remove.append(child)
                    if child.attrib.get("name", "") == "black":
                        break
            for elm in elms_to_remove:
                spine.remove(elm)
            for child in spine.iter():
                if child.attrib:
                    if child.attrib.get("name", "").split(".")[-1] in ["wav", "flac"]:
                        child.tag = "audio"

        tree.write(file_name, encoding='utf-8', xml_declaration=True)

    def _addFormats(self, resources_tag):
        format_tag = self._addTag(resources_tag, "format")
        format_tag["width"] = self.project.width
        format_tag["height"] = self.project.height
        format_tag["id"] = "r0"
        format_tag["frameDuration"] = "%d/%ds" % (
            self.project.frame_rate_den, self.project.frame_rate_num)

    def _addResources(self, resources_tag):
        for clip_id, clip in self.project.clips.items():
            resource = clip.resource
            if isinstance(resource, ClipFile):
                asset = self._addTag(resources_tag, "asset")
                asset["name"] = clip.name
                asset["id"] = clip_id
                asset["src"] = "file://" + resource.resource_path
                asset["hasVideo"] = 1
                asset["duration"] = self._formatTime(clip.duration)
            elif isinstance(resource, Project):
                self._addEmbeddedTimeline(clip_id, resources_tag)

    def _addEmbeddedTimeline(self, clip_id, resources_tag):
        clip = project.clips[clip_id]
        embedded_project = clip.resource
        writer = FcpXmlWriter(embedded_project)
        if embedded_project not in self.added_embedded_resources:
            self.added_embedded_resources.add(embedded_project)
            writer._addResources(resources_tag)

        media = self._addTag(resources_tag, "media")
        media["name"] = clip.name
        media["id"] = clip_id
        writer._addSequence(media, True)

    def _addLibrary(self, library_tag):
        event = self._addTag(library_tag, "event")
        event["name"] = "Timeline 1"
        project_tag = self._addTag(event, "project")
        project_tag["name"] = "Timeline 1"
        self._addSequence(project_tag, False)

    def _addSequence(self, project_tag, wrap_in_clip):
        sequence = self._addTag(project_tag, "sequence")
        sequence["format"] = "r0"
        spine = self._addTag(sequence, "spine")

        if wrap_in_clip:
            wrapper = self._addTag(spine, "clip")
            wrapper["duration"] = self._formatTime(self._getProjectLength())
        else:
            wrapper = spine

        index = 0
        for track in self.project.tracks:
            self._addTrack(track, wrapper, index)
            index += 1

    # Computes the project length, given by the latest out-time it contains.

    def _getProjectLength(self):
        latest_out = 0
        for track in self.project.tracks:
            for entry in track.entries:
                latest_out = max(latest_out, entry.out_time)
        return latest_out

    def _addTrack(self, track, spine, index):
        offset = 0
        for entry in track.entries:
            clip = entry.clip
            duration = entry.out_time - entry.in_time + \
                (1.0 / self.project.frame_rate)

            if clip is None:
                if ADD_GAP_NODES:
                    clip_node = self._addTag(spine, "gap")
                else:
                    offset += duration
                    continue
            else:
                resource = clip.resource
                if isinstance(resource, ClipFile):
                    clip_tag_name = "audio" if track.is_audio else "video"
                    clip_node = self._addTag(spine, clip_tag_name)
                elif isinstance(resource, Project):
                    clip_node = self._addTag(spine, "ref-clip")
                    clip_node["srcEnable"] = "audio" if track.is_audio else "video"
                    self._addFakeTimemap(clip_node)

                clip_node["name"] = clip.name
                clip_node["ref"] = clip.clip_id

            clip_node["start"] = self._formatTime(entry.in_time)
            clip_node["duration"] = self._formatTime(duration)
            clip_node["offset"] = self._formatTime(offset)
            if index > 0:
                clip_node["lane"] = index

            offset += duration

    # Adds a <timeMap> node that forces Resolve to create a compound clip, although the speed we set is 100%.

    def _addFakeTimemap(self, clip_node):
        timemap = self._addTag(clip_node, "timeMap")

        timept = self._addTag(timemap, "timept")
        timept["time"] = "0/1s"
        timept["value"] = "0/1s"

        timept = self._addTag(timemap, "timept")
        timept["time"] = "1/1s"
        timept["value"] = "10/10s"

    def _formatTime(self, seconds):
        # TODO: use frame rate? Use GCD?

        return "%d/%ds" % (round(float(seconds) * self.project.frame_rate_num),    self.project.frame_rate_num)

    def _addTag(self, node, tagName, **attributes):
        tag = self.xml.new_tag(tagName, **attributes)
        node.append(tag)
        return tag


def convert(file_name, out=None):
    reader = KdenliveReader(force_time_frames=args.timing_as_framenumber, force_time_timestamp=args.timing_as_timestamp, legacy_format=args.legacy_format)
    project = reader.read(file_name)
    is_legacy = args.legacy_format
    print(reader.version)
    if reader.version == 1:
        is_legacy = True
    writer = FcpXmlWriter(project, is_legacy) 
    writer.write(output_filename)


# =============================================================================
#  Main
# =============================================================================
if __name__ == "__main__":
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument("FILE", nargs="+", help="Input file")
    arg_parser.add_argument("-o", "--output", required=False,
                            help="Output file. If the output filename is not given, replaces the extension of the input file with .fcpxml.")
    arg_parser.add_argument("-e", "--embedded-mlt-to-compound-clip", required=False, action="store_true", help=("Whether to turn an embedded .kdenlive file into a compound clip, or leave it alone."
                                                                                                                " Doesn't work in all cases. A possible workaround is to convert the embedded .kdenlive independently,"
                                                                                                                " import it as a timeline in Resolve, and replacing the missing clip with the imported timeline."))
    arg_parser.add_argument("-g", "--add-gap-nodes", required=False, action="store_true",
                            help="Unclear whether these are necessary, since every clip has an absolute offset anyway.")
    arg_parser.add_argument("-l", "--legacy-format", required=False, action="store_true",
                            help="Use the old, unmodified format with unremoved lines.")

    arg_parser.add_argument("-f", "--timing-as-framenumber", required=False, action="store_true",
                            help="Force timing information to be parsed as framenumber.")

    arg_parser.add_argument("-t", "--timing-as-timestamp", required=False, action="store_true",
                            help="Force timing information to be parsed as timestamp.")
    args = arg_parser.parse_args()

    EMBEDDED_MLT_TO_COMPOUND_CLIP = args.embedded_mlt_to_compound_clip
    ADD_GAP_NODES = args.add_gap_nodes

    for file_args in args.FILE:
        for file in glob.glob(file_args):
            if file:
                if args.output:
                    if os.path.isdir(args.output):
                        output_filename = os.path.join(args.output, os.path.splitext(file.rsplit(os.sep, 1)[-1])[0] + ".fcpxml")
                    else:
                        output_filename = args.output
                else:
                    output_filename = os.path.splitext(file)[0] + ".fcpxml"
                convert(file, output_filename)
            else:
                print("Skipping glob %s as it doesn't exist" % file_args)
