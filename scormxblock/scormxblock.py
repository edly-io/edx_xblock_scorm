import concurrent.futures
import json
import hashlib
import mimetypes
import re
import os
import stat
import logging
import pkg_resources
import shutil
import xml.etree.ElementTree as ET

from django.conf import settings
from django.core.files.storage import default_storage
from django.template import Context, Template
from django.utils import timezone
from webob import Response
from openedx.core.djangoapps.site_configuration import helpers as configuration_helpers

from xblock.completable import CompletableXBlockMixin
from xblock.core import XBlock
from xblock.fields import Scope, String, Float, Boolean, Dict, DateTime, Integer
from xblock.fragment import Fragment


# Make '_' a no-op so we can scrape strings
_ = lambda text: text

log = logging.getLogger(__name__)

SCORM_ROOT = os.path.join(settings.MEDIA_ROOT, "scormxblockmedia")
SCORM_URL = os.path.join(settings.MEDIA_URL, "scormxblockmedia")
MAX_WORKERS = getattr(settings, "THREADPOOLEXECUTOR_MAX_WORKERS", 10)
ENABLE_PUBLISH_FAILED_SCORM_SCORE = settings.FEATURES.get('ENABLE_PUBLISH_FAILED_SCORM_SCORE', False)


class ScormXBlock(XBlock, CompletableXBlockMixin):
    display_name = String(
        display_name=_("Display Name"),
        help=_("Display name for this module"),
        default="Scorm",
        scope=Scope.settings,
    )
    scorm_file = String(
        display_name=_("Upload scorm file"),
        scope=Scope.settings,
    )
    path_index_page = String(
        display_name=_("Path to the index page in scorm file"),
        scope=Scope.settings,
    )
    scorm_file_meta = Dict(scope=Scope.content)
    version_scorm = String(
        default="SCORM_12",
        scope=Scope.settings,
    )
    # save completion_status for SCORM_2004
    lesson_status = String(scope=Scope.user_state, default="not attempted")
    success_status = String(scope=Scope.user_state, default="unknown")
    data_scorm = Dict(scope=Scope.user_state, default={})
    lesson_score = Float(scope=Scope.user_state, default=0)
    weight = Float(default=1, scope=Scope.settings)
    has_score = Boolean(
        display_name=_("Scored"),
        help=_(
            "Select False if this component will not receive a numerical score from the Scorm"
        ),
        default=True,
        scope=Scope.settings,
    )
    icon_class = String(
        default="video",
        scope=Scope.settings,
    )
    width = Integer(
        display_name=_("Display Width (px)"),
        help=_("Width of iframe, if empty, the default 100%"),
        scope=Scope.settings,
    )
    height = Integer(
        display_name=_("Display Height (px)"),
        help=_("Height of iframe"),
        default=450,
        scope=Scope.settings,
    )
    open_in_pop_up = Boolean(
        display_name=_("Open in Pop-up"),
        help=_(
            "Select True if you want learners to click on 'view course' button and then open scorm content in a pop-up window."
            "Select False if you want the scorm content to open in an IFrame in the current page. "
        ),
        default=False,
        scope=Scope.settings
    )

    has_author_view = True

    def resource_string(self, path):
        """Handy helper for getting resources from our kit."""
        data = pkg_resources.resource_string(__name__, path)
        return data.decode("utf8")

    def student_view(self, context=None):
        context_html = self.get_context_student()
        template = self.render_template("static/html/scormxblock.html", context_html)
        frag = Fragment(template)
        frag.add_css(self.resource_string("static/css/scormxblock.css"))
        frag.add_javascript(self.resource_string("static/js/src/scormxblock.js"))
        settings = {"version_scorm": self.version_scorm, "scorm_path": context_html["scorm_file_path"]}
        frag.initialize_js("ScormXBlock", json_args=settings)
        return frag

    def studio_view(self, context=None):
        context_html = self.get_context_studio()
        template = self.render_template("static/html/studio.html", context_html)
        frag = Fragment(template)
        frag.add_css(self.resource_string("static/css/scormxblock.css"))
        frag.add_javascript(self.resource_string("static/js/src/studio.js"))
        frag.initialize_js("ScormStudioXBlock")
        return frag

    def author_view(self, context=None):
        html = self.render_template("static/html/author_view.html", context)
        frag = Fragment(html)
        return frag

    def _delete_local_storage(self):
        path = self.local_storage_path
        if os.path.exists(path):
            shutil.rmtree(path)

    @property
    def local_storage_path(self):
        return os.path.join(
            SCORM_ROOT, self.location.org, self.location.course, self.location.block_id
        )

    @property
    def s3_storage(self):
        return "S3" in default_storage.__class__.__name__

    def get_remote_path(self, local_path):
        return "".join(
            [self._file_storage_path(), local_path.replace(self.local_storage_path, "")]
        )

    @XBlock.handler
    def studio_submit(self, request, suffix=""):
        self.display_name = request.params["display_name"]
        self.width = request.params["width"]
        self.height = request.params["height"]
        self.open_in_pop_up = request.params["open_in_pop_up"]
        self.has_score = request.params["has_score"]
        self.icon_class = "problem" if self.has_score else "video"

        if hasattr(request.params["file"], "file"):
            scorm_file = request.params["file"].file

            # First, save scorm file in the storage for mobile clients
            self.scorm_file_meta["sha1"] = self.get_sha1(scorm_file)
            self.scorm_file_meta["name"] = scorm_file.name
            self.scorm_file_meta["path"] = self._file_storage_path()
            self.scorm_file_meta["last_updated"] = timezone.now().strftime(
                DateTime.DATETIME_FORMAT
            )
            self.scorm_file_meta["size"] = scorm_file.size

            self._unpack_files(scorm_file)
            self.update_subdir_permissions()
            self.set_fields_xblock()
            if self.s3_storage:
                self._store_unziped_files_to_s3()
                # Removed locally unzipped files once we have store them on S3
                self._delete_local_storage()

        # changes made for juniper (python 3.5)
        return Response(
            json.dumps({"result": "success"}),
            content_type="application/json",
            charset="utf8",
        )

    def update_subdir_permissions(self):
        """
        Extends existing permissions of all the the sub-directories with the Owner Execute permission (S_IXUSR).

        All sub-directories of the scorm-package must have executable permissions for the Directory Owner otherwise
        Studio will raise Permission Denied error on scorm package upload.
        """
        for path, subdirs, files in os.walk(self.local_storage_path):
            for name in subdirs:
                dir_path = os.path.join(path, name)
                st = os.stat(dir_path)
                os.chmod(dir_path, st.st_mode | stat.S_IXUSR)

    def _unpack_files(self, scorm_file):
        """
        Unpacks zip file using unzip system utility
        """
        # Now unpack it into SCORM_ROOT to serve to students later
        self._delete_local_storage()
        local_path = self.local_storage_path
        os.makedirs(local_path)

        if hasattr(scorm_file, "temporary_file_path"):
            os.system(
                "unzip {} -d {}".format(scorm_file.temporary_file_path(), local_path)
            )
        else:
            temporary_path = os.path.join(SCORM_ROOT, scorm_file.name)
            temporary_zip = open(temporary_path, "wb")
            scorm_file.open()
            temporary_zip.write(scorm_file.read())
            temporary_zip.close()
            os.system("unzip {} -d {}".format(temporary_path, local_path))
            os.remove(temporary_path)

    def _fix_content_type(self, file_path):
        """
        Sometimes content type of file returned by mimetypes module is bytes object instead of string
        which fails content type validation of boto3 and boto3 would not upload file instead throws
        `botocore.exceptions.ParamValidationError: Parameter validation failed:`
        This method fixes such content types by changing their type from bytes to string
        """
        _content_type, __ = mimetypes.guess_type(file_path)
        try:
            str_type = _content_type.decode("utf-8")
            ext = file_path.split(".")[-1]
            mimetypes.add_type(str_type, "." + ext)
        except (UnicodeDecodeError, AttributeError):
            pass

    def _upload_file(self, file_path):
        self._fix_content_type(file_path)
        path = self.get_remote_path(file_path)
        with open(file_path, "rb") as content_file:
            default_storage.save(path, content_file)
        log.info('S3: "{}" file stored at "{}"'.format(file_path, path))

    def _delete_existing_files(self, path):
        """
        Recusively delete all files under given path
        """
        dir_names, file_names = default_storage.listdir(path)
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            tracker_futures = []
            for file_name in file_names:
                file_path = "/".join([path, file_name])
                tracker_futures.append(
                    executor.submit(default_storage.delete, file_path)
                )
                log.info('S3: "{}" file deleted'.format(file_path))

        for dir_name in dir_names:
            dir_path = "/".join([path, dir_name])
            self._delete_existing_files(dir_path)

    def _store_unziped_files_to_s3(self):
        """"""
        self._delete_existing_files(self._file_storage_path())
        local_path = self.local_storage_path
        file_paths = []
        for path, subdirs, files in os.walk(local_path):
            for name in files:
                file_paths.append(os.path.join(path, name))

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            tracker_futures = {
                executor.submit(self._upload_file, file_path): file_path
                for file_path in file_paths
            }
            for future in concurrent.futures.as_completed(tracker_futures):
                file_path = tracker_futures[future]
                try:
                    future.result()
                except Exception as exc:
                    log.info(
                        "S3: upload of %r generated an exception: %s" % (file_path, exc)
                    )

    @XBlock.json_handler
    def scorm_get_value(self, data, suffix=""):
        name = data.get("name")
        if name in ["cmi.core.lesson_status", "cmi.completion_status"]:
            return {"value": self.lesson_status}
        elif name == "cmi.success_status":
            return {"value": self.success_status}
        elif name in ["cmi.core.score.raw", "cmi.score.raw"]:
            return {"value": self.lesson_score * 100}
        else:
            return {"value": self.data_scorm.get(name, "")}

    @XBlock.json_handler
    def scorm_set_value(self, data, suffix=""):
        context = {"result": "success"}
        name = data.get("name")

        if name in ["cmi.core.lesson_status", "cmi.completion_status"]:
            self.lesson_status = data.get("value")
            if self.has_score and data.get("value") in [
                "completed",
                "failed",
                "passed",
            ]:
                self.publish_grade()
                context.update({"lesson_score": self.lesson_score})

        elif name == "cmi.success_status":
            self.success_status = data.get("value")
            if self.has_score:
                if self.success_status == "unknown":
                    self.lesson_score = 0
                self.publish_grade()
                context.update({"lesson_score": self.lesson_score})
        elif name in ["cmi.core.score.raw", "cmi.score.raw"] and self.has_score:
            self.lesson_score =  round(float(data.get("value", 0)) / 100.0, 2)
            self.publish_grade()
            context.update({"lesson_score": self.lesson_score})
        else:
            self.data_scorm[name] = data.get("value", "")

        completion_status = self.get_completion_status()
        context.update({"completion_status": completion_status})

        # publish completion
        if completion_status in ["passed", "failed", "completed"]:
            self.emit_completion(1.0)

        return context

    def publish_grade(self):
        if not ENABLE_PUBLISH_FAILED_SCORM_SCORE and (
            self.lesson_status == "failed" or (
                self.version_scorm == "SCORM_2004" and self.success_status in ["failed", "unknown"]
            )
        ):
            self.runtime.publish(
                self,
                "grade",
                {
                    "value": 0,
                    "max_value": self.weight,
                },
            )
        else:
            self.runtime.publish(
                self,
                "grade",
                {
                    "value": self.lesson_score,
                    "max_value": self.weight,
                },
            )

    def max_score(self):
        """
        Return the maximum score possible.
        """
        return self.weight if self.has_score else None

    def get_context_studio(self):
        return {
            "field_display_name": self.fields["display_name"],
            "field_scorm_file": self.fields["scorm_file"],
            "field_has_score": self.fields["has_score"],
            "field_width": self.fields["width"],
            "field_height": self.fields["height"],
            "field_open_in_pop_up": self.fields["open_in_pop_up"],
            "scorm_xblock": self,
        }

    def get_context_student(self):
        scorm_file_path = ""
        if self.scorm_file:
            scorm_file_path = self.scorm_file

        return {
            "scorm_file_path": scorm_file_path,
            "completion_status": self.get_completion_status(),
            "scorm_xblock": self,
        }

    def render_template(self, template_path, context):
        template_str = self.resource_string(template_path)
        template = Template(template_str)
        return template.render(Context(context))

    def set_fields_xblock(self):

        self.path_index_page = "index.html"
        try:
            tree = ET.parse("{}/imsmanifest.xml".format(self.local_storage_path))
        except IOError:
            pass
        else:
            namespace = ""
            for node in [
                node
                for _, node in ET.iterparse(
                    "{}/imsmanifest.xml".format(self.local_storage_path),
                    events=["start-ns"],
                )
            ]:
                if node[0] == "":
                    namespace = node[1]
                    break
            root = tree.getroot()

            if namespace:
                resource = root.find(
                    "{{{0}}}resources/{{{0}}}resource".format(namespace)
                )
                schemaversion = root.find(
                    "{{{0}}}metadata/{{{0}}}schemaversion".format(namespace)
                )
            else:
                resource = root.find("resources/resource")
                schemaversion = root.find("metadata/schemaversion")

            if resource is not None:
                self.path_index_page = resource.get("href")
            if (schemaversion is not None) and (
                re.match("^1.2$", schemaversion.text) is None
            ):
                self.version_scorm = "SCORM_2004"
            else:
                self.version_scorm = "SCORM_12"

        self.scorm_file = os.path.join(
            SCORM_URL,
            "{}/{}/{}/{}".format(
                self.location.org,
                self.location.course,
                self.location.block_id,
                self.path_index_page,
            ),
        )

    def get_completion_status(self):
        completion_status = self.lesson_status
        if self.version_scorm == "SCORM_2004" and self.success_status != "unknown":
            completion_status = self.success_status
        return completion_status

    def _file_storage_path(self):
        """
        Get file path of storage.
        """
        path = "scormxblockmedia/{loc.org}/{loc.course}/{loc.block_id}".format(
            loc=self.location,
        )
        return path

    def get_sha1(self, file_descriptor):
        """
        Get file hex digest (fingerprint).
        """
        block_size = 8 * 1024
        sha1 = hashlib.sha1()
        # changes made for juniper (python 3.5)
        while True:
            block = file_descriptor.read(block_size)
            if not block:
                break
            sha1.update(block)
        file_descriptor.seek(0)
        return sha1.hexdigest()

    def student_view_data(self):
        """
        Inform REST api clients about original file location and it's "freshness".
        Make sure to include `student_view_data=scormxblock` to URL params in the request.
        """
        if self.scorm_file and self.scorm_file_meta:
            return {
                "last_modified": self.scorm_file_meta.get("last_updated", ""),
                "scorm_data": default_storage.url(self._file_storage_path()),
                "size": self.scorm_file_meta.get("size", 0),
                "index_page": self.path_index_page,
            }
        return {}

    @staticmethod
    def workbench_scenarios():
        """A canned scenario for display in the workbench."""
        return [
            (
                "ScormXBlock",
                """<vertical_demo>
                <scormxblock/>
                </vertical_demo>
             """,
            ),
        ]
