import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;

/** A controlled console application, NOT a Minecraft server. */
public class NovaTestServer {
    public static void main(String[] args) throws Exception {
        System.out.println("NOVA Java fixture ready");
        System.out.flush();
        try (var input = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8))) {
            String line;
            while ((line = input.readLine()) != null) {
                if (line.equals("stop") || line.equals("shutdown")) {
                    System.out.println("NOVA Java fixture stopped cleanly");
                    break;
                }
                System.out.println("Received: " + line);
                System.out.flush();
            }
        }
    }
}
