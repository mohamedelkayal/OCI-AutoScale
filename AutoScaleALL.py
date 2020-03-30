import oci
import logging
import datetime
import threading
import time
import sys
import traceback

PredefinedTag = "Schedule"
AnyDay = "AnyDay"
Weekend = "Weekend"
WeekDay = "WeekDay"
Daysofweek = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# OCI Configuration
configfile = "~\\oci\\config_autoscale"
ComputeShutdownMethod = "SOFTSTOP"

# Configure logging output
# logging.basicConfig(filename="~/autoscale.log", format='%(asctime)s %(message)s', level=logging.INFO)
logging.basicConfig(format='%(asctime)s %(message)s', level=logging.WARNING)

# Logging for this script so we don't have to use the same settings as the OCI module
logger = logging.getLogger('Info')
logger.setLevel(logging.INFO)

Action = "All"  # Default, do all up/on and down/off scaling actions

# Get profile from command line
if len(sys.argv) >= 2:
    profile = sys.argv[1]
else:
    profile = 'DEFAULT'

if len(sys.argv) == 3:
    if sys.argv[2].upper() == "UP":
        Action = "Up"
    if sys.argv[2].upper() == "DOWN":
        Action = "Down"

logger.info("Starting Auto Scaling script, executing {} actions".format(Action))


class AutonomousThread(threading.Thread):
    def __init__(self, threadID, ID, NAME, CPU):
        threading.Thread.__init__(self)
        self.threadID = threadID
        self.ID = ID
        self.NAME = NAME
        self.CPU = CPU

    def run(self):
        logger.info("Starting Autonomous DB {} and after that scaling to {} cpus".format(self.NAME, self.CPU))
        response = database.start_autonomous_database(autonomous_database_id=self.ID).data
        while response.lifecycle_state != "AVAILABLE":
            response = database.get_autonomous_database(autonomous_database_id=self.ID).data
            time.sleep(5)
        logger.info("Autonomous DB {} started, re-scaling to {} cpus".format(self.NAME, self.CPU))
        dbupdate = oci.database.models.UpdateAutonomousDatabaseDetails()
        dbupdate.cpu_core_count = self.CPU
        response = database.update_autonomous_database(autonomous_database_id=self.ID, update_autonomous_database_details=dbupdate)


class PoolThread(threading.Thread):
    def __init__(self, threadID, ID, NAME, INSTANCES):
        threading.Thread.__init__(self)
        self.threadID = threadID
        self.ID = ID
        self.NAME = NAME
        self.INSTANCES = INSTANCES

    def run(self):
        logger.info(
            "Starting Instance Pool {} and after that scaling to {} instances".format(self.NAME, self.INSTANCES))
        response = pool.start_instance_pool(instance_pool_id=self.ID).data
        while response.lifecycle_state != "RUNNING":
            response = pool.get_instance_pool(instance_pool_id=self.ID).data
            time.sleep(5)
        logger.info("Instance pool {} started, re-scaling to {} instances".format(self.NAME, self.INSTANCES))
        pooldetails = oci.core.models.UpdateInstancePoolDetails()
        pooldetails.size = self.INSTANCES
        response = pool.update_instance_pool(instance_pool_id=self.ID, update_instance_pool_details=pooldetails).data

# Check credentials and enabled regions
config = oci.config.from_file(configfile, profile_name=profile)
identity = oci.identity.IdentityClient(config)
user = identity.get_user(config["user"]).data
RootCompartmentID = user.compartment_id
print("Logged in as: {} @ {}  (Profile={})".format(user.description, config["region"], profile))

regions = identity.list_region_subscriptions(config["tenancy"]).data
regions_list = []

for region in regions:
    regions_list.append(region.region_name)

logger.info("Enabled regions: {}".format(regions_list))

threads = []  # Thread array for async AutonomousDB start and rescale
tcount = 0

# Get Current Day, time
DayOfWeek = datetime.datetime.today().weekday()  # Day of week as a number
Day = Daysofweek[DayOfWeek]  # Day of week as string
CurrentHour = datetime.datetime.now().hour
logger.info("Day of week: {} - Current hour: {}".format(Day, CurrentHour))

# Array start with 0 so decrease CurrentHour with 1
CurrentHour = CurrentHour-1
search_result = ""
time.sleep(2)
print("{:25} {:40} {:29} {}".format("Resource type", "Definition", "Compartment path", "Action taken"))

# Traverse the returned object list to build the full compartment path
def traverse(compartments, parent_id, parent_path, compartment_list):
    next_level_compartments = [c for c in compartments if c.compartment_id == parent_id]
    for compartment in next_level_compartments:
        if compartment.name[0:17] != 'casb_compartment.' and compartment.lifecycle_state == 'ACTIVE':
            path = parent_path+'/'+compartment.name
            compartment_list.append(
                dict(id=compartment.id, name=compartment.name, path=path, state=compartment.lifecycle_state)
            )
            traverse(compartments, compartment.id, path, compartment_list)
    return compartment_list

def get_compartment_list(base_compartment_id):
    # Get list of all compartments below given base
    compartments = oci.pagination.list_call_get_all_results(
        identity.list_compartments, base_compartment_id,
        compartment_id_in_subtree=True).data

    # Got the flat list of compartments, now construct full path of each which makes it much easier to locate resources
    base_compartment_name = 'Root'
    base_path = '/root'

    compartment_list = [dict(id=base_compartment_id, name=base_compartment_name, path=base_path, state='Root')]
    compartment_list = traverse(compartments, base_compartment_id, base_path, compartment_list)
    compartment_list = sorted(compartment_list, key=lambda c: c['path'].lower())

    return compartment_list

compartment_list = get_compartment_list(config['tenancy'])

for c in regions_list:
    config['region'] = c
    compute = oci.core.ComputeClient(config)
    database = oci.database.DatabaseClient(config)
    pool = oci.core.ComputeManagementClient(config)

    # Find (almost) all resources with a Schedule Tag
    search = oci.resource_search.ResourceSearchClient(config)
    query = "query all resources where (definedTags.namespace = '{}')".format(PredefinedTag)

    sdetails = oci.resource_search.models.StructuredSearchDetails()
    sdetails.query = query

    result = search.search_resources(search_details=sdetails, limit=1000).data

    # All the items with a schedule are now collected.
    # Let's go thru them and find / validate the correct schedule

    for resource in result.items:
        error_status_flag = True
        num_attempts = 0
        if resource.resource_type == "Instance" or resource.resource_type == "DbSystem" or resource.resource_type == "AutonomousDatabase":
            while error_status_flag != False and num_attempts < 3:

                try:
                    print(f"{resource.resource_type:25} {resource.display_name:41}", end = "")
                    for cc in compartment_list:
                        if resource.compartment_id == cc['id']:
                            print(f"{cc['path']:30}", end="")
                            break

                    schedule = resource.defined_tags[PredefinedTag]
                    ActiveSchedule = ""
                    if AnyDay in schedule:
                        ActiveSchedule = schedule[AnyDay]
                    if DayOfWeek < 6:  # check for weekday / weekend
                        if WeekDay in schedule:
                            ActiveSchedule = schedule[WeekDay]
                    else:
                        if Weekend in schedule:
                            ActiveSchedule = schedule[Weekend]
                    if Day in schedule:  # Check for day specific tag (today)
                        ActiveSchedule = schedule[Day]

                    # Check is the active schedule contains exactly 24 numbers for each hour of the day and the resource not deleted
                    if resource.lifecycle_state != "DELETED":
                        try:
                            schedulehours = ActiveSchedule.split(",")
                            if len(schedulehours) != 24:
                                print("Error with schedule, not correct amount of hours")
                                ActiveSchedule = ""
                                error_status_flag = False
                        except:
                            ActiveSchedule = ""
                            logger.error("Error with schedule of {}".format(resource.display_name))
                            error_status_flag = False

                        # if schedule validated, let see if we can apply the new schedule to the resource
                        if ActiveSchedule != "":
                            # Execute On/Off operations for compute VMs
                            if resource.resource_type == "Instance":
                                if int(schedulehours[CurrentHour]) == 0 or int(schedulehours[CurrentHour]) == 1:
                                    resourceDetails = compute.get_instance(instance_id=resource.identifier).data

                                    # Only perform action if VM Instance, ignoring any BM instances.
                                    if resourceDetails.shape[:2] == "VM":
                                        if resourceDetails.lifecycle_state == "RUNNING" and int(schedulehours[CurrentHour]) == 0:
                                            if Action == "All" or Action == "Down":
                                                response = compute.instance_action(instance_id=resource.identifier, action=ComputeShutdownMethod)
                                                print("Initiated Compute shutdown")
                                            else:
                                                print("Correct state")

                                        elif resourceDetails.lifecycle_state == "STOPPED" and int(schedulehours[CurrentHour]) == 1:
                                            if Action == "All" or Action == "Up":
                                                response = compute.instance_action(instance_id=resource.identifier, action="START")
                                                print("Initiated Compute startup")
                                            else:
                                                print("Correct state")
                                        else:
                                            print("Correct state")

                                    # Execute CPU Scale Up/Down operations for Database BMs
                                    if resourceDetails.shape[:2] == "BM":
                                        if int(schedulehours[CurrentHour]) > 1 and int(schedulehours[CurrentHour]) < 53:
                                            if resourceDetails.cpu_core_count > int(schedulehours[CurrentHour]):
                                                if Action == "All" or Action == "Down":
                                                    dbupdate = oci.database.models.UpdateDbSystemDetails()
                                                    dbupdate.cpu_core_count = int(schedulehours[CurrentHour])
                                                    response = database.update_db_system(db_system_id=resource.identifier, update_db_system_details=dbupdate)
                                                    print("Initiated Compute shutdown")
                                            elif resourceDetails.cpu_core_count < int(schedulehours[CurrentHour]):
                                                if Action == "All" or Action == "Up":
                                                    dbupdate = oci.database.models.UpdateDbSystemDetails()
                                                    dbupdate.cpu_core_count = int(schedulehours[CurrentHour])
                                                    response = database.update_db_system(db_system_id=resource.identifier, update_db_system_details=dbupdate)
                                                    print("Initiate Computed startup")
                                            else:
                                                print("Correct state")
                                        else:
                                            print("Correct state")


                            elif resource.resource_type == "DbSystem":
                                resourceDetails = database.get_db_system(db_system_id=resource.identifier).data

                                # Execute On/Off operations for Database VMs
                                if resourceDetails.shape[:2] == "VM":
                                    for i in range(resourceDetails.node_count):
                                        if i > 0:
                                            print(f"{resource.resource_type:25} {resource.display_name:40} {'Next node of the previouse comp.':29} ", end="")

                                        dbnodedetails = database.list_db_nodes(compartment_id=resource.compartment_id, db_system_id=resource.identifier).data[i]
                                        if int(schedulehours[CurrentHour]) == 0 or int(schedulehours[CurrentHour]) == 1:
                                            if dbnodedetails.lifecycle_state == "AVAILABLE" and int(schedulehours[CurrentHour]) == 0:
                                                if Action == "All" or Action == "Down":
                                                    response = database.db_node_action(db_node_id=dbnodedetails.id, action="STOP")
                                                    print("Initiated DB shutdown")
                                                else:
                                                    print("Correct state")

                                            elif dbnodedetails.lifecycle_state == "STOPPED" and int(schedulehours[CurrentHour]) == 1:
                                                if Action == "All" or Action == "Up":
                                                    response = database.db_node_action(db_node_id=dbnodedetails.id, action="START")
                                                    print("Initiated DB startup")
                                                else:
                                                    print("Correct state")
                                            else:
                                                print("Correct state")

                            # Execute CPU Scale Up/Down operations for Database BMs
                            elif resource.resource_type == "AutonomousDatabase":
                                if int(schedulehours[CurrentHour]) >= 0 and int(schedulehours[CurrentHour]) < 129:
                                    resourceDetails = database.get_autonomous_database(
                                        autonomous_database_id=resource.identifier).data

                                    # Autonomous DB is running request is to stop the database
                                    if resourceDetails.lifecycle_state == "AVAILABLE" and int(schedulehours[CurrentHour]) == 0:
                                        if Action == "All" or Action == "Down":
                                            response = database.stop_autonomous_database(autonomous_database_id=resource.identifier)
                                            print("Initiated Autonomous DB shutdown")
                                        else:
                                            print("Correct state")

                                    elif resourceDetails.lifecycle_state == "STOPPED" and int(schedulehours[CurrentHour]) > 0:
                                        if Action == "All" or Action == "Up":
                                            # Autonomous DB is stopped and needs to be started with same amount of CPUs configured
                                            if resourceDetails.cpu_core_count == int(schedulehours[CurrentHour]):
                                                response = database.start_autonomous_database(autonomous_database_id=resource.identifier)
                                                print("Initiated Autonomous DB startup")

                                            # Autonomous DB is stopped and needs to be started, after that it requires CPU change
                                            if resourceDetails.cpu_core_count != int(schedulehours[CurrentHour]):
                                                tcount = tcount+1
                                                thread = AutonomousThread(tcount, resource.identifier, resource.display_name, int(schedulehours[CurrentHour]))
                                                thread.start()
                                                threads.append(thread)
                                        else:
                                            print("Correct state")
                                    else:
                                        print("Correct state")
                            error_status_flag = False
                except:
                    num_attempts += 1
                    if num_attempts != 3:
                        if "currently being modified, try again later" in traceback.format_exc():
                            print("Resources currently being modified, will try after 1 min")
                        elif "has a conflicting state of FAILED." in traceback.format_exc():
                            print("WARNING, the resource has a conflicting state of FAILED ")
                        else:
                            traceback.print_exc()
                        time.sleep(1)
                    else:
                        print('Failed with all the attempts')
# Wait for any AutonomousDB and Instance Pool Start and rescale tasks completed
for t in threads:
    t.join()

print("All scaling tasks done")
