edx_xblock_scorm
=========================
XBlock to display SCORM content within the Open edX LMS.  Editable within Open edx Studio. Will save student state and report scores to the progress tab of the course.
Currently supports SCORM 1.2 and SCORM 2004 standard.

Block displays SCORM which saved as `File -> Export -> Web Site -> Zip File`

Block displays SCORM which saved as `File -> Export -> SCORM 1.2`


Installation
------------

Install package for juniper release

    pip install -e git+https://github.com/edly-io/edx_xblock_scorm.git@juniper#egg=edx_xblock_scorm==juniper

For Ironwood release make the following change
    
    pip install -e git+https://github.com/edly-io/edx_xblock_scorm.git@ironwood#egg=edx_xblock_scorm==ironwood

Note: for OpenEdx releases prior ginkgo add required variables to CMS configuration ```<edx-platform-path>/cms/envs/aws.py```:

```
MEDIA_ROOT = ENV_TOKENS.get('MEDIA_ROOT', '/edx/var/edxapp/media/')
MEDIA_URL = ENV_TOKENS.get('MEDIA_URL', '/media/')
```

# Usage
* Add `scormxblock` to the list of advanced modules in the advanced settings of a course.
* Add a `scorm` component to your Unit. 
* Upload a zip file containing your content package.  The `imsmanifest.xml` file must be at the root of the zipped package (i.e., make sure you don't have an additional directory at the root of the Zip archive which can handle if e.g., you select an entire folder and use Mac OS X's compress feature).
* Publish your content as usual.

To make it default xblock for all the courses so that it will appear under advance problems tab.
go to `edx-platform/cms/envs/common.py` and add `scormxblock` in `ADVANCED_PROBLEM_TYPES`
i.e    

     ADVANCED_PROBLEM_TYPES = [
        {
            'component': 'scormxblock',
            'boilerplate_name': None
        }
    ] 

Testing
-------

Assuming `scormxblock` is installed as above, you can run tests like so:

    $ pytest --pyargs scormxblock
