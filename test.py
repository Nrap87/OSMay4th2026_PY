import json
old = json.load(open("data.json"))["routes"]
new_routes = json.load(open("new_routes.json"))["routes"]
old_set = {(min(r["from_planet"], r["to_planet_id"]), max(r["from_planet"], r["to_planet_id"]), r["route_type"]) for r in old}
new_set = {(min(r["from_planet"], r["to_planet_id"]), max(r["from_planet"], r["to_planet_id"]), r["route_type"]) for r in new_routes}
print("In new but not old:", new_set - old_set)
print("In old but not new:", old_set - new_set)