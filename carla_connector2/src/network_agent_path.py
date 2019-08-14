import random
import numpy as np
import math

class NetworkAgentPath:
    def __init__(self, drunc, min_points, interval):
        self.drunc = drunc
        self.min_points = min_points
        self.interval = interval
        self.route_points = []

    @staticmethod
    def rand_path(drunc, min_points, interval):
        spawn_point = None
        route_paths = None
        while not spawn_point or len(route_paths) < 1:
            spawn_point = drunc.rand_network_route_point()
            route_paths = drunc.network.get_next_route_paths(spawn_point, min_points - 1, interval)

        path = NetworkAgentPath(drunc, min_points, interval)
        path.route_points = random.choice(route_paths)
        return path

    def resize(self):
        while len(self.route_points) < self.min_points:
            next_points = self.drunc.network.get_next_route_points(self.route_points[-1], self.interval)
            if len(next_points) == 0:
                return False
            self.route_points.append(random.choice(next_points))
        return True

    def cut(self, position):
        cut_index = 0
        min_offset = 100000.0
        for i in range(len(self.route_points) / 2):
            route_point = self.route_points[i]
            offset = position - self.drunc.network.get_route_point_position(route_point)
            offset = offset.length() 
            if offset < min_offset:
                min_offset = offset
            if offset < 0.5:
                cut_index = i + 1

        self.route_points = self.route_points[cut_index:]

    def get_position(self, index=0):
        return self.drunc.network.get_route_point_position(self.route_points[index])

    def get_yaw(self, index=0):
        pos = self.drunc.network.get_route_point_position(self.route_points[index])
        next_pos = self.drunc.network.get_route_point_position(self.route_points[index + 1])
        return np.rad2deg(math.atan2(next_pos.y - pos.y, next_pos.x - pos.x))
