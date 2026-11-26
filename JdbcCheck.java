// JdbcCheck.java - prove JDBC connectivity to Oracle Free 23ai
// Needs the Oracle JDBC driver jar (ojdbc11.jar) and Java 11 or later.
//   javac JdbcCheck.java
//   java -cp .:ojdbc11.jar JdbcCheck "jdbc:oracle:thin:@//localhost:10022/FREEPDB1" bank_app
// The password is read from the ORACLE_PASSWORD environment variable.
import java.sql.*;

public class JdbcCheck {
    public static void main(String[] a) throws Exception {
        String pw = System.getenv("ORACLE_PASSWORD");
        long t0 = System.nanoTime();
        try (Connection c = DriverManager.getConnection(a[0], a[1], pw)) {
            System.out.printf("connected in %.1f ms, driver %s, database %s%n",
                (System.nanoTime() - t0) / 1e6,
                c.getMetaData().getDriverVersion(),
                c.getMetaData().getDatabaseProductVersion().split("\n")[0]);
            try (Statement s = c.createStatement();
                 ResultSet r = s.executeQuery("SELECT COUNT(*) FROM bank.txn")) {
                r.next();
                System.out.println("bank.txn rows = " + r.getInt(1));
            }
        }
    }
}
