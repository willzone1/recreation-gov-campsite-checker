#!/usr/bin/env python3

import argparse
import json
import logging
import sys
import csv
from datetime import datetime, timedelta
from dateutil import rrule
from itertools import count, groupby

import requests
from fake_useragent import UserAgent

import smtplib, ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import configparser

config = configparser.ConfigParser()
config.read_file(open('config.cfg'))

# CAMPSITE_TYPE = "STANDARD NONELECTRIC"

LOG = logging.getLogger(__name__)
formatter = logging.Formatter("%(asctime)s - %(process)s - %(levelname)s - %(message)s")
sh = logging.StreamHandler()
sh.setFormatter(formatter)
LOG.addHandler(sh)

BASE_URL = "https://www.recreation.gov"
AVAILABILITY_ENDPOINT = "/api/camps/availability/campground/"
MAIN_PAGE_ENDPOINT = "/api/camps/campgrounds/"

INPUT_DATE_FORMAT = "%Y-%m-%d"
ISO_DATE_FORMAT_REQUEST = "%Y-%m-%dT00:00:00.000Z"
ISO_DATE_FORMAT_RESPONSE = "%Y-%m-%dT00:00:00Z"

SUCCESS_EMOJI = "🏕"
FAILURE_EMOJI = "❌"

headers = {"User-Agent": UserAgent().random}


def send_email(subject, body, address_modifier=""):
    sender_email = config.get('EMAIL', 'sender_email')
    receiver_email = config.get('EMAIL', "receiver_email{}".format(address_modifier))

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = sender_email
    message["To"] = receiver_email

    text = """\
    {}\n
    FROM: {}""".format(body, config.get('SERVER', 'name'))

    message.attach(MIMEText(text, "plain"))

    # Create a secure SSL context
    context = ssl.create_default_context()

    with smtplib.SMTP_SSL(config.get('EMAIL', 'sender_smtp_server'), config.get('EMAIL', 'sender_smtp_port'), context=context) as server:
        server.login(sender_email, config.get('EMAIL', 'sender_password'))
        server.sendmail(sender_email, receiver_email, message.as_string())


def format_date(date_object, format_string=ISO_DATE_FORMAT_REQUEST):
    """
    This function doesn't manipulate the date itself at all, it just
    formats the date in the format that the API wants.
    """
    date_formatted = datetime.strftime(date_object, format_string)
    return date_formatted


def site_date_to_human_date(date_string):
    date_object = datetime.strptime(date_string, ISO_DATE_FORMAT_RESPONSE)
    return format_date(date_object, format_string=INPUT_DATE_FORMAT)


def send_request(url, params):
    resp = requests.get(url, params=params, headers=headers)
    tries = 1
    while resp.status_code != 200:
        resp = requests.get(url, params=params, headers=headers)
        tries += 1
        if resp.status_code != 200 and tries > 5:
            send_email("RETRYING {}".format(resp.status_code), "RETRYING because ERROR, {} code received from {}: {}".
                       format(resp.status_code, url, resp.text), "_error")
            raise RuntimeError(
                "failedRequest",
                "ERROR, {} code received from {}: {}".format(
                    resp.status_code, url, resp.text
                ),
            )

    return resp.json()


def get_park_information(park_id, start_date, end_date, campsite_type=None):
    """
    This function consumes the user intent, collects the necessary information
    from the recreation.gov API, and then presents it in a nice format for the
    rest of the program to work with. If the API changes in the future, this is
    the only function you should need to change.

    The only API to get availability information is the `month?` query param
    on the availability endpoint. You must query with the first of the month.
    This means if `start_date` and `end_date` cross a month bounday, we must
    hit the endpoint multiple times.

    The output of this function looks like this:

    {"<campsite_id>": [<date>, <date>]}

    Where the values are a list of ISO 8601 date strings representing dates
    where the campsite is available.

    Notably, the output doesn't tell you which sites are available. The rest of
    the script doesn't need to know this to determine whether sites are available.
    """

    # Get each first of the month for months in the range we care about.
    start_of_month = datetime(start_date.year, start_date.month, 1)
    months = list(rrule.rrule(rrule.MONTHLY, dtstart=start_of_month, until=end_date))

    # Get data for each month.
    api_data = []
    for month_date in months:
        params = {"start_date": format_date(month_date)}
        LOG.debug("Querying for {} with these params: {}".format(park_id, params))
        url = "{}{}{}/month?".format(BASE_URL, AVAILABILITY_ENDPOINT, park_id)
        resp = send_request(url, params)
        api_data.append(resp)

    # Collapse the data into the described output format.
    # Filter by campsite_type if necessary.
    data = {}
    for month_data in api_data:
        for campsite_id, campsite_data in month_data["campsites"].items():
            available = []
            a = data.setdefault(campsite_id, [])
            for date, availability_value in campsite_data["availabilities"].items():
                if availability_value != "Available":
                    continue
                if campsite_type and campsite_type != campsite_data["campsite_type"]:
                    continue
                available.append(date)
            if available:
                a += available

    return data


def get_name_of_park(park_id):
    url = "{}{}{}".format(BASE_URL, MAIN_PAGE_ENDPOINT, park_id)
    resp = send_request(url, {})
    return resp["campground"]["facility_name"]


def get_num_available_sites(park_information, start_date, end_date, nights=None):
    availabilities_filtered = []
    maximum = len(park_information)

    num_available = 0
    num_days = (end_date - start_date).days
    dates = [end_date - timedelta(days=i) for i in range(1, num_days + 1)]
    dates = set(format_date(i, format_string=ISO_DATE_FORMAT_RESPONSE) for i in dates)

    if nights not in range(1, num_days + 1):
        nights = num_days
        LOG.debug("Setting number of nights to {}.".format(nights))

    for site, availabilities in park_information.items():
        # List of dates that are in the desired range for this site.
        desired_available = []

        for date in availabilities:
            if date not in dates:
                continue
            desired_available.append(date)

        if not desired_available:
            continue

        appropriate_consecutive_ranges = consecutive_nights(desired_available, nights)

        if appropriate_consecutive_ranges:
            num_available += 1
            LOG.debug("Available site {}: {}".format(num_available, site))

        for r in appropriate_consecutive_ranges:
            start, end = r
            availabilities_filtered.append(
                {"site": int(site), "start": start, "end": end}
            )

    return num_available, maximum, availabilities_filtered


def consecutive_nights(available, nights):
    """
    Returns a list of dates from which you can start that have
    enough consecutive nights.

    If there is one or more entries in this list, there is at least one
    date range for this site that is available.
    """
    ordinal_dates = [
        datetime.strptime(dstr, ISO_DATE_FORMAT_RESPONSE).toordinal()
        for dstr in available
    ]
    c = count()
    nights_ordinal = datetime.strptime(str(nights), "%d").toordinal()
    consective_ranges = list(
        list(g) for _, g in groupby(ordinal_dates, lambda x: x - next(c))
    )

    long_enough_consecutive_ranges = []
    for r in consective_ranges:
        # Skip ranges that are too short.
        if r[0] < nights_ordinal:
            continue
        for start_index in range(0, len(r) - nights):
            start_nice = format_date(
                datetime.fromordinal(r[start_index]), format_string=INPUT_DATE_FORMAT
            )
            end_nice = format_date(
                datetime.fromordinal(r[start_index + nights]),
                format_string=INPUT_DATE_FORMAT,
            )
            long_enough_consecutive_ranges.append((start_nice, end_nice))

    return long_enough_consecutive_ranges


def check_park(park_id, start_date, end_date, campsite_type, nights=None):
    park_information = get_park_information(
        park_id, start_date, end_date, campsite_type
    )
    LOG.debug(
        "Information for park {}: {}".format(
            park_id, json.dumps(park_information, indent=2)
        )
    )
    name_of_park = get_name_of_park(park_id)
    current, maximum, availabilities_filtered = get_num_available_sites(
        park_information, start_date, end_date, nights=nights
    )
    return current, maximum, availabilities_filtered, name_of_park


def output_human_output(parks, start_date, end_date, nights):
    out = []
    availabilities = False
    for park_id in parks:
        current, maximum, _, name_of_park = check_park(
            park_id, start_date, end_date, campsite_type=None, nights=nights
        )
        if current:
            emoji = SUCCESS_EMOJI
            availabilities = True
        else:
            emoji = FAILURE_EMOJI

        out.append(
            "{} {} ({}): {} site(s) available out of {} site(s) for {} nights".format(
                emoji, name_of_park, park_id, current, maximum, nights
            )
        )

    if availabilities:
        print(
            "There are campsites available from {} to {}!!!".format(
                start_date.strftime(INPUT_DATE_FORMAT),
                end_date.strftime(INPUT_DATE_FORMAT),
            )
        )
        send_email(subject="CAMPSITES AVAILABLE from {} to {}!!!".format(
                start_date.strftime(INPUT_DATE_FORMAT),
                end_date.strftime(INPUT_DATE_FORMAT)),
                body="CAMPSITES AVAILABLE from {} to {}!\n{}".format(
                start_date.strftime(INPUT_DATE_FORMAT),
                end_date.strftime(INPUT_DATE_FORMAT), out))
    else:
        print("There are no campsites available :( from ", start_date, " to ", end_date, " for ", nights, " nights")
    print("\n".join(out))
    return availabilities


def output_json_output(parks, start_date, end_date, nights):
    park_to_availabilities = {}
    availabilities = False
    for park_id in parks:
        current, _, availabilities_filtered, _ = check_park(
            park_id, start_date, end_date, campsite_type, nights=nights
        )
        if current:
            availabilities = True
            park_to_availabilities[park_id] = availabilities_filtered

    print(json.dumps(park_to_availabilities))

    return availabilities


def main(parks, start_date, end_date, nights, json_output=False):
    if json_output:
        return output_json_output(parks, start_date, end_date, nights)
    else:
        return output_human_output(parks, start_date, end_date, nights)


def valid_date(s):
    try:
        return datetime.strptime(s, INPUT_DATE_FORMAT)
    except ValueError:
        msg = "Not a valid date: '{0}'.".format(s)
        raise argparse.ArgumentTypeError(msg)


def positive_int(i):
    i = int(i)
    if i <= 0:
        msg = "Not a valid number of nights: {0}".format(i)
        raise argparse.ArgumentTypeError(msg)
    return i


def read_csv(filename):
    with open(filename, newline='') as datescsv:
        dates = list(csv.reader(datescsv, delimiter=',', quotechar='|'))
    return dates


if __name__ == "__main__":
    # Start date has format YYYY-MM-DD
    # End date also has format YYYY-MM-DD. You expect to leave this day, not stay the night.
    # campsite-type can be set if you want to filter by a type of campsite. For example "STANDARD NONELECTRIC"
    # park IDs can be found at the end of the campground URL

    campsite_type = None

    net_avail = []

    nights = 1

    for trip in read_csv(filename="search.csv"):
        start_date = valid_date(trip[0])
        end_date = valid_date(trip[1])
        parks = trip[2].split(";")

        if start_date.date() >= datetime.today().date():
            try:
                code = 0 if main(parks, start_date, end_date, nights, json_output=False) else 1
                net_avail.append(code)
            except Exception:
                print("Something went wrong")
                LOG.exception("Something went wrong")
                raise

    sys.exit(min(net_avail))
