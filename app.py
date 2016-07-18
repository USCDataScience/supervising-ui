#!/usr/bin/env python
# encoding: utf-8
# Copyright 2016 Information Retrieval and Data Science (IRDS) Group,
# University of Southern California (USC), Los Angeles
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function
from argparse import ArgumentParser
import os
import json
import sys
import sqlite3
import logging
from datetime import datetime
import urllib

from flask import Flask, render_template, request, abort, send_file, redirect, Response
app = Flask(__name__)

# Constants
DB_FILE = "db.sqlite"
SETTINGS_FILE = "settings.json"
LOGS_FILE = "logs.log"
CREATE_TABLE_STMT = "CREATE TABLE IF NOT EXISTS data (" \
    "url text PRIMARY KEY, " \
    "label text, " \
    "last_modified datetime DEFAULT current_timestamp, " \
    " UNIQUE (url) ON CONFLICT IGNORE )" # Ignore it to preserve previous labels
INSERT_STMT = "INSERT INTO data VALUES (?, ?, ?)"
UPDATE_STMT = "UPDATE data SET label=?, last_modified=datetime() WHERE url=?"
SELECT_UNLABELLED = "SELECT * FROM data WHERE label IS NULL"
SELECT_LABELLED = "SELECT * FROM data WHERE label IS NOT NULL"
GET_STMT = "SELECT * FROM data WHERE url = ?"
LOG_LEVEL = logging.DEBUG


service = None # will be initialized from CLI args

@app.route("/")
def webpage():
    url = request.args.get('url')
    if not url:
        # redirect with url query param so that user can navigate back later
        next_rec = service.get_next_unlabelled()
        if next_rec:
            return redirect("/?url=%s" % (urllib.quote(next_rec['url'])))
        else:
            featured_content = "No Unlabelled Record Found."
    else:
        featured_content = get_next(url)
    data = {
        'featured_content': featured_content,
        'status': service.overall_status()
    }
    return render_template('index.html', **data)

@app.route("/proxy")
def document():
    url = request.args.get('url')
    if not url or not os.path.exists(url):
        return abort(400, "File %s not found " % url)
    return send_file(url)

@app.route("/update", methods=['POST'])
def update():
    data = request.form
    url = data['url']
    labels = data.getlist('label')
    assert labels
    assert url
    count = service.update_record(url, labels)
    if count > 0:
        return redirect(location="/")
    else:
        return abort(400, "Failed... No records updated")

@app.route("/settings")
def get_settings():
    return json.dumps(service.settings)

@app.route("/download.csv")
def download():
    recs = service.query_recs(SELECT_LABELLED + " ORDER BY last_modified DESC", first_only=False)
    recs = map(lambda r: "\t".join([r['last_modified'], r['url'], r['label']])
                            + "\n", recs)
    return Response(recs, mimetype='text/csv')

def get_next(url=None):
    next_rec = service.get_record(url)
    url = next_rec['url']
    template_name = '%s.html' % service.settings['type']
    data_url = url if url.startswith('http') else "/proxy?url=%s" % urllib.quote(next_rec['url'])
    data = {
        'data_url' : data_url,
        'url': url,
        'task': service.settings['task']
    }
    return render_template(template_name, **data)

class DbService(object):

    def __init__(self, workdir, input_file):
        self.workdir = workdir
        print("Work Dir : %s" % workdir)
        logs_fn = os.path.join(workdir, LOGS_FILE)
        settings_fn = os.path.join(workdir, SETTINGS_FILE)
        if not os.path.exists(settings_fn):
            print("Error: Settings file not found, looked at: %s" % settings_fn)
            sys.exit(2)

        print("Logs are being stored at %s ." % logs_fn)
        self.log = logging
        self.log.basicConfig(filename=logs_fn, level=LOG_LEVEL)
        with open(settings_fn) as f:
            self.settings = json.load(f)
            self.log.debug("Loaded the settings : %s" % (self.settings))
        self.db = self.connect_db()
        self.log.info("Work Dir %s" % workdir)
        if input_file:
            if not os.path.exists(input_file):
                self.log.info("Error: Input file not found : %s" % input_file)
                sys.exit(1)
            with open(input_file, 'r') as input:
                urls_or_paths = filter(lambda y: y, map(lambda x: x.strip(), input))
                count = self.insert_if_not_exists(urls_or_paths)
                self.log.info("Inserted %d new records from %s file." % (count, input_file))
        else:
            self.log.info("No new inputs are supplied")

    def connect_db(self):
        db_file = os.path.join(self.workdir, DB_FILE)
        self.log.info("Connecting to database file at %s" % db_file)
        db = sqlite3.connect(db_file, check_same_thread=False)
        def dict_factory(cursor, row): # map tuples to dictionary with column names
            d = {}
            for idx, col in enumerate(cursor.description):
                d[col[0]] = row[idx]
            return d
        db.row_factory = dict_factory
        cursor = db.cursor()
        cursor.execute(CREATE_TABLE_STMT)
        db.commit()
        cursor.close()
        return db

    def insert_if_not_exists(self, urls):
        count = 0
        cursor = self.db.cursor()
        for url in urls:
            values = (url, None, datetime.now())
             # assumption: if rec exists, DB will IGNORE IT
            res = cursor.execute(INSERT_STMT, values)
            count += 1
        self.db.commit()
        cursor.close()
        return count

    def update_record(self, url, labels):
        self.log.info("Updating %s with %s" % (url, labels))
        cur = self.db.execute(UPDATE_STMT, (",".join(labels), url))
        count = cur.rowcount
        self.log.info("Rows Updated = %d" % count)
        cur.close()
        self.db.commit()
        return count

    def get_next_unlabelled(self):
        return self.query_recs(SELECT_UNLABELLED + " ORDER BY RANDOM() LIMIT 1",
                                first_only=True)

    def get_record(self, url):
        return self.db.execute(GET_STMT, (url,)).fetchone()

    def query_recs(self, query, first_only=True):
        cur = self.db.execute(query)
        return cur.fetchone() if first_only else cur

    def get_count(self, query):
        assert " * " in query
        query = query.replace(" * ", " COUNT(*) as COUNT ")
        return self.db.execute(query).fetchone()['COUNT']

    def overall_status(self):
        pending = self.get_count("SELECT * FROM data WHERE label IS NULL")
        total = self.get_count("SELECT * FROM data")
        return {'total': total, 'pending': pending, 'done': total - pending}

    def __del__(self):
        if hasattr(self, 'db') and self.db:
            self.log.info("Committing before exit.")
            self.db.commit()
            self.db = None

if __name__ == "__main__":
    parser = ArgumentParser(description="Web UI for Labelling images")
    parser.add_argument("-i", "--input", help="Path to to input file which has list of paths, one per line. (Optional)")
    parser.add_argument("-w", "--work-dir", help="Work Directory. (Required)", required=True)
    parser.add_argument("-p", "--port", type=int, help="Bind port. (Optional)", default=8080)
    args = vars(parser.parse_args())
    host = '0.0.0.0'
    service = DbService(args['work_dir'], args['input'])
    print("Starting on  %s %s/" % (host, args['port']))
    app.run(host=host, port=args['port'])
