public class BuggyCode {
    public static void main(String[] args) {

        int[] numbers = {10, 20, 30, 40, 50};

        int sum = 0;

        for (int i = 0; i <= numbers.length; i++) {
            sum = sum + numbers[i];
        }

        double average = sum / numbers.length;

        System.out.println("Sum: " + sum);
        System.out.println("Average: " + average);
    }
}
