import os.path
import json
from glob import glob
from collections import OrderedDict
import time
import requests

ROOTDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATEDIR = os.path.join(ROOTDIR, 'templates')

class BaseProject(object):
    '''
    Base class storing task actions.

    Each @property method corresponds to a DoIt task of the same name.
    In practice, platformify is usually the only one that most projects will need
    to override.
    '''

    # A dictionary of conditional commands to run for package updaters.
    # The key is a file name. If that file exists, then its value will be run in the
    # project build directory to update the corresponding lock file.
    updateCommands = {
        'composer.json': 'composer update --prefer-dist --ignore-platform-reqs --no-interaction --no-suggest',
        'Pipfile': 'pipenv update',
        'Gemfile': 'bundle update',
        'package.json': 'npm update',
    }
    
    '''Amount of seconds to delay test execution after target_url is available.
    This is due to the fact that some apps take longer to really deploy than what is reported back to the status check.
    '''
    TEST_DELAY = 10

    def __init__(self, name):
        self.name = name
        self.builddir = os.path.join(TEMPLATEDIR, self.name, 'build/')
        # Parses the github authorization token from env var by default.
        self.github_token = os.getenv('GITHUB_TOKEN', None)

       
        # A list containing function references which will be executed against a test url, bootstrapped with a basic smoke test.
        self.TEST_FUNCTIONS = [self.basic_smoke_test]

    @property
    def cleanup(self):
        return ['rm -rf {0}'.format(self.builddir)]

    @property
    def init(self):
        if hasattr(self, 'github_name'):
            name = self.github_name
        else:
            name = self.name.replace('_', '-')
        return ['git clone git@github.com:platformsh-templates/{0}.git {1}'.format(
            name, self.builddir)
        ]

    @property
    def update(self):
        actions = [
            'cd {0} && git checkout master && git pull --prune'.format(self.builddir)
        ]

        actions.extend(self.package_update_actions())

        return actions

    @property
    def platformify(self):
        """
        The default implementation of this method will
        1) Copy the contents of the files/ directory in the project over the
           application, replacing what's there.
        2) Apply any *.patch files found in the project directory, in alphabetical order.

        Individual projects may expand on these tasks as needed.
        """
        actions = ['rsync -aP {0} {1}'.format(
            os.path.join(TEMPLATEDIR, self.name, 'files/'),  self.builddir
        )]
        patches = glob(os.path.join(TEMPLATEDIR, self.name, "*.patch"))
        for patch in patches:
            actions.append('cd {0} && patch -p1 < {1}'.format(
                self.builddir, patch)
            )

        # In some cases the package updater needs to be run after we've platform-ified the
        # template, so run it a second time. Worst case it's a bit slower to build but doesn't
        # hurt anything.
        actions.extend(self.package_update_actions())

        return actions

    @property
    def branch(self):
        return [
            'cd {0} && if git rev-parse --verify --quiet update; then git checkout master && git branch -D update; fi;'.format(
                self.builddir),
            'cd {0} && git checkout -b update'.format(self.builddir),
            # git commit exits with 1 if there's nothing to update, so the diff-index check will
            # short circuit the command if there's nothing to update with an exit code of 0.
            'cd {0} && git add -A && git diff-index --quiet HEAD || git commit -m "Update to latest upstream"'.format(
                self.builddir),
        ]

    @property
    def push(self):
        return ['cd {0} && if [ `git rev-parse update` != `git rev-parse master` ] ; then git checkout update && git push --force -u origin update; fi'.format(
            self.builddir)
        ]

    def pull_request(self, token=None):
        """
        Creates a pull request from the "update" branch to master.
        """
        self.set_github_token(token)

        authorization_header = {"Authorization": "token " + self.github_token}

        pulls_api_url = 'https://api.github.com/repos/platformsh-templates/{0}/pulls'.format(self.name)

        body = {"head": "update", "base": "master", "title": "Update to latest upstream"}
        response = requests.post(pulls_api_url, headers=authorization_header, data=json.dumps(body))
        return response.status_code in [201, 422]

    def test(self, token=None):
        """
        Calls all the tests defined in self.TEST_FUNCTIONS on the test urls.
        """
        self.set_github_token(token)

        urls_to_test = self.get_test_urls()
        if not urls_to_test:
            print("No pull requests to test for {0}".format(self.name))

        results = []
        print(f"Running {len(self.TEST_FUNCTIONS)} tests.")
        for test in self.TEST_FUNCTIONS:
            for url in urls_to_test:
                results.append(test(url))

        return all(results)

    def merge_pull_request(self, token=None):
        """
        Merges latest pull request.
        """
        self.set_github_token(token)

        authorization_header = {"Authorization": "token " + self.github_token}

        pulls_api_url = 'https://api.github.com/repos/platformsh-templates/{0}/pulls'.format(self.name)
        pull = requests.get(pulls_api_url, headers=authorization_header).json()[0]
        print(pull["number"])
        merge_url = pulls_api_url + "/" + str(pull["number"]) + "/merge"
        response = requests.put(merge_url, headers=authorization_header)
        print(response.text, response.url)
        return response.status_code in [200, 204]


    @staticmethod
    def basic_smoke_test(url):
        """
        Basic smoke test for a single PR. Only checks for a 200 OK response.
        """
        try:
            response = requests.get(url)
        except requests.exceptions.SSLError:
            response = requests.get(url, verify=False)

        if response.status_code != 200:
            print("Test failed on {0} with code {1}".format(response.url, response.status_code))
        return response.status_code == 200


    def package_update_actions(self):
        """
        Generates a list of package updater commands based on the updateCommands property.

        :return: List of package update commands to include.
        """
        actions = []
        for file, command in self.updateCommands.items():
            actions.append('cd {0} && [ -f {1} ] && {2} || echo "No {1} file found, skipping."'.format(self.builddir, file, command))

        return actions

    def modify_composer(self, mod_function):
        """
        Wordpress requires more Composer modification than can be done
        with the Composer command line.  This function modifies the composer.json
        file as raw JSON instead.
        """

        with open('{0}/composer.json'.format(self.builddir), 'r') as f:
        # The OrderedDict means that the property orders in composer.json will be preserved.
            composer = json.load(f, object_pairs_hook=OrderedDict)

        composer = mod_function(composer)

        with open('{0}/composer.json'.format(self.builddir), 'w') as out:
            json.dump(composer, out, indent=2)

    def get_test_urls(self):
        """
        Returns URLs of environments integrated to active pull requests.
        """
        authorization_header = {"Authorization": "token " + self.github_token}

        pulls_api_url = 'https://api.github.com/repos/platformsh-templates/{0}/pulls'.format(self.name)
        pulls = requests.get(pulls_api_url, headers=authorization_header)
        urls = []
        for pull in pulls.json():
            statuses_api_url = pull["statuses_url"]
            url = ""
            while not url:
                status = requests.get(statuses_api_url, headers=authorization_header)
                try:
                    data = status.json()[0]
                    url = data["target_url"]

                    if data["status"] != "success":
                        print("Pull request {0} is still building on Platform.sh".format(pull["url"]))
                        url = ""

                except Exception as e:
                    print("Pull request {0} was not built on Platform.sh".format(pull["url"]))
            
            print(f"Pull request {url} has finished building on Platform.sh")
            urls.append(url)
        
        # Delays execution by specified amount of seconds    
        print(f"Delaying execution of tests by {self.TEST_DELAY} seconds.")
        time.sleep(self.TEST_DELAY)
        return urls
   
    def set_github_token(self, token):
       # CMD line token overrides env var token.
       if token is not None:
           self.github_token = token
       # If token is still not set, alert user. Pd: Cannot failt gracefully as it is not on main task code.
       if self.github_token is None:
           print("Github token not provided. Please provide via --token argument or via GITHUB_TOKEN env var.")
