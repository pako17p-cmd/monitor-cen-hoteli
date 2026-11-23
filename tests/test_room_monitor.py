import sqlite3
import unittest
import room_monitor

class RoomCountParserTests(unittest.TestCase):
    def test_polish_room_messages(self):
        self.assertEqual(room_monitor.parse_room_count("<div>Zostały 4 pokoje!</div>"),4)
        self.assertEqual(room_monitor.parse_room_count("Pozostały tylko 2 pokoje"),2)
        self.assertEqual(room_monitor.parse_room_count("Został 1 pokój"),1)
        self.assertEqual(room_monitor.parse_room_count('<span class="badge">4 OSTATNIE POKOJE</span>'),4)
    def test_missing_or_ambiguous_message_is_not_zero(self):
        self.assertIsNone(room_monitor.parse_room_count("Oferta jest dostępna"))
        self.assertIsNone(room_monitor.parse_room_count("Brak informacji o pokojach"))
        self.assertIsNone(room_monitor.parse_room_count("Zostały 4 pokoje, a w innym miejscu 2 pokoje"))
    def test_state_round_trip_and_independent_settings(self):
        c=sqlite3.connect(":memory:")
        c.row_factory=sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("CREATE TABLE hotels(id INTEGER PRIMARY KEY,name TEXT)")
        c.execute("INSERT INTO hotels VALUES(1,'Test')")
        room_monitor.ensure_schema(c)
        room_monitor.set_settings(c,1,"WAW","2027-01-10",7,True,False)
        state=room_monitor.export_state(c)
        room_monitor.clear(c)
        room_monitor.restore(c,state)
        row=c.execute("SELECT monitor_enabled,email_enabled FROM room_monitors").fetchone()
        self.assertEqual(tuple(row),(1,0))
        c.close()

if __name__ == "__main__":
    unittest.main()
